"""运行后端检测 —— detect_backend。

按环境优先级一次性决定后端类型，不做运行时回退。
T10：独立模块。Full Backend Protocol / SpawnRequest / 三种实现见本包其余模块。
"""

from __future__ import annotations

import os
import shutil

from core.team.types import BackendType


def detect_backend() -> BackendType:
    """按优先级一次性决定队员执行后端。

    1. $TMUX 已设置 → tmux（当前在 tmux 会话内）
    2. $TERM_PROGRAM == "iTerm.app" 且本机有 it2 → iterm2
    3. PATH 里有 tmux 二进制 → tmux（外部 spawn 新 session）
    4. 否则 → in-process
    """
    if os.environ.get("TMUX"):
        return BackendType.TMUX

    if os.environ.get("TERM_PROGRAM") == "iTerm.app" and shutil.which("it2"):
        return BackendType.ITERM2

    if shutil.which("tmux"):
        return BackendType.TMUX

    return BackendType.IN_PROCESS


def backend_display_name(backend: BackendType) -> str:
    """给 TUI / 命令输出用的可读名称。"""
    return backend.value


__all__ = ["backend_display_name", "detect_backend"]
