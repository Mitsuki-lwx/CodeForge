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
from dataclasses import dataclass
from pathlib import Path

from core.permissions.dangerous import DangerousCommandDetector
from core.permissions.modes import (
    DecisionEffect,
    PermissionMode,
    ToolCategory,
    mode_decide,
)
from core.permissions.rules import (
    RuleEngine,
    extract_content,
    extract_paths,
    is_safe_command,
)
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

        # Layer 2（读）：只读工具也要过路径沙箱。
        # 旧实现把 read-only 的自动放行（下方 Layer 1）放在沙箱之前，于是
        # `read_file C:/Users/x/.ssh/id_rsa` 直接 allow —— 读密钥零审批，
        # 且内容会进 LLM 上下文发往第三方 API。越界读判 `ask`（不是 deny）：
        # 交互式用户可批准，无人值守由策略代答，两边都不丢能力。
        #
        # 这里**只标记不立即返回**：让 Layer 3 的显式规则（用户在
        # `.codeforge/settings.json` 里写的 allow）有机会覆盖沙箱 ——
        # 沙箱是默认护栏，不是不可协商的墙。真正的 `ask` 落在 Layer 4c。
        read_outside = False
        read_outside_reason = ""
        if tool_category == "read" and self.mode != PermissionMode.BYPASS:
            for target in extract_paths(args):
                sandbox_result = self._sandbox.check(target)
                if not sandbox_result.ok:
                    read_outside = True
                    read_outside_reason = sandbox_result.reason
                    break

        # Layer 1: 安全只读命令自动放行
        if tool_category == "read" and tool_is_read_only and not read_outside:
            return Decision(effect="allow", reason="Read-only tool")

        # Layer 1: 命令类工具 → 安全命令检查
        if is_command and content:
            if is_safe_command(content):
                return Decision(effect="allow", reason="Safe read-only command")

        # Layer 1b: 危险命令黑名单
        if is_command and content:
            hit = self._detector.detect(content)
            if hit.is_dangerous:
                return Decision(
                    effect="deny", reason=f"Dangerous command: {hit.reason}"
                )

        # Layer 2（写）：路径沙箱
        if tool_category == "write":
            for target in extract_paths(args):
                sandbox_result = self._sandbox.check(target)
                if not sandbox_result.ok:
                    if self.mode == PermissionMode.BYPASS:
                        pass  # bypass 绕过沙箱
                    else:
                        return Decision(
                            effect="deny",
                            reason=f"Path sandbox: {sandbox_result.reason}",
                        )

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

        # Layer 4c: 越界读（Layer 2 标记下来的）。
        # 必须排在 Layer 4 之前 —— 矩阵里 read 档位是 allow，否则越界读会被
        # "Mode default: allow" 直接放行，沙箱形同虚设。
        if read_outside:
            return Decision(
                effect="ask",
                reason=f"Path sandbox: {read_outside_reason}",
            )

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

    def load_rules(self, cwd: str | Path) -> None:
        """加载项目级 + 用户级规则（会话装配时调用一次）。

        不调用的话 Layer 3 永远匹配空规则集 —— 用户在
        `.codeforge/settings.json` 里写的 deny 规则会静默失效。
        """
        self._rule_engine.load_rules(cwd)

    def add_project_rule(
        self, cwd: str | Path, tool_name: str, content: str, action: DecisionEffect
    ) -> bool:
        """HITL 选择「允许并保存」后落盘到项目规则。"""
        if action not in ("allow", "deny", "ask"):
            return False
        return self._rule_engine.save_project_rule(cwd, tool_name, content, action)

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
