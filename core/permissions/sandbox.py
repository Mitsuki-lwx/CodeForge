"""路径沙箱 —— 限制文件读写工具只能在允许的目录范围内操作。

默认严格限制在工作目录及其子目录内。
支持相对路径解析、符号链接规范化、跨平台 Windows/Linux 路径处理。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SandboxResult:
    ok: bool
    reason: str


class PathSandbox:
    """路径沙箱。

    检查文件操作目标路径是否在允许的范围内。
    """

    def __init__(
        self,
        work_dir: str | Path = ".",
        enabled: bool = True,
        work_dir_only: bool = True,
    ) -> None:
        self._work_dir = Path(work_dir).resolve()
        self.enabled = enabled
        self.work_dir_only = work_dir_only

    def update_work_dir(self, work_dir: str | Path) -> None:
        """更新工作目录（如 Agent 切换目录时）。"""
        self._work_dir = Path(work_dir).resolve()

    def check(self, target_path: str) -> SandboxResult:
        """检查路径是否在允许范围内。

        Args:
            target_path: 要检查的文件路径（绝对或相对）

        Returns:
            SandboxResult(ok, reason)
        """
        if not self.enabled:
            return SandboxResult(ok=True, reason="Sandbox disabled")

        if not target_path or not target_path.strip():
            return SandboxResult(ok=True, reason="Empty path")

        try:
            # 规范化：处理 ~、相对路径、..
            resolved = Path(os.path.expanduser(target_path))
            if not resolved.is_absolute():
                resolved = (self._work_dir / resolved)
            resolved = resolved.resolve()
        except (ValueError, OSError, RuntimeError):
            return SandboxResult(
                ok=False,
                reason=f"Cannot resolve path: {target_path}",
            )

        # 检查是否在工作目录内
        try:
            resolved.relative_to(self._work_dir)
            return SandboxResult(ok=True, reason="")
        except ValueError:
            pass

        if self.work_dir_only:
            return SandboxResult(
                ok=False,
                reason=f"Path outside work directory: {target_path}",
            )

        return SandboxResult(
            ok=False,
            reason=f"Path outside allowed directories: {target_path}",
        )
