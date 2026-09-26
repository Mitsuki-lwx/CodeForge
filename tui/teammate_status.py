"""后台任务 / 队友状态行（只读可见性）。

把 `BackgroundTaskManager` 里**运行中**的任务渲染成提示符下方的一行文本，
让"派出去之后什么都看不到"变成"看得见它在跑、跑到第几步"。

纯函数、无 IO、无全局状态 —— 可脱离终端单测。

- 为什么只显示 RUNNING、为什么没有"等输入"：见 `docs/spec_teammate_status.md` §3
- 为什么用 bottom_toolbar：见同文档 §4.1
"""

from __future__ import annotations

import shutil
import time
from typing import Any

from core.task.manager import TaskStatus

# 显示名 / 宽度截断 / 耗时**与 /agents 下钻共用同一份实现**（core/task/view.py）：
# 上一轮真链路已证明"显示名取值顺序"是坑（`AgentTool.name` 常常为空），
# 两份规则一定会漂移。这里只做转发，行为逐字不变（既有测试守住）。
from core.task.view import (
    NAME_MAX_COLS as _NAME_MAX_COLS,
)
from core.task.view import (
    clip as _clip,
)
from core.task.view import (
    disp_width,
    display_name,
    format_elapsed,
)

# 最近动作的显示上限（**列**，中文算 2 列）
_ACTIVITY_MAX_COLS = 12
# 终端再窄也至少按这么宽排版（否则截出来没意义）
_MIN_WIDTH = 20
_FALLBACK_WIDTH = 80


def _name_of(bt: Any) -> str:
    """显示名：`name` → 任务文本首行 → id 短码（实现见 `core/task/view.py`）。"""
    return display_name(bt, _NAME_MAX_COLS)


def format_task(bt: Any, now: float) -> str:
    """单个任务的一段：`alice 12s·8步·Grep`。

    步数为 0、最近动作为空时**省略该段** —— 不显示 `0步` 这类噪音。
    """
    start = float(getattr(bt, "start_time", 0.0) or 0.0)
    end = float(getattr(bt, "end_time", 0.0) or 0.0)
    parts = [f"{_name_of(bt)} {format_elapsed((end or now) - start)}"]

    steps = int(getattr(bt, "tool_count", 0) or 0)
    if steps > 0:
        parts.append(f"{steps}步")

    activity = str(getattr(bt, "last_activity", "") or "").strip()
    if activity:
        parts.append(_clip(activity, _ACTIVITY_MAX_COLS))

    return "·".join(parts)


def _resolve_width(width: int | None) -> int:
    if width is None:
        try:
            width = shutil.get_terminal_size(fallback=(_FALLBACK_WIDTH, 24)).columns
        except Exception:  # noqa: BLE001 —— 取不到就用兜底值，状态行不该因此消失
            width = _FALLBACK_WIDTH
    # 留 1 列余量，避免刚好顶满触发换行
    return max(_MIN_WIDTH, int(width) - 1)


def build_status_text(
    task_mgr: Any, *, now: float | None = None, width: int | None = None
) -> str | None:
    """渲染状态行。

    **没有任何运行中的任务时返回 `None`** —— 提示符下方不占行，
    这样"没有后台任务"时的界面与引入本功能之前**逐字一致**。

    Args:
        task_mgr: `BackgroundTaskManager`（`None` 也安全）。
        now: 注入当前时间（测试用；默认 `time.monotonic()`，与 `start_time` 同源）。
        width: 注入终端列数（测试用）。
    """
    if task_mgr is None:
        return None
    try:
        tasks = list(task_mgr.list())  # 已按 start_time 升序
    except Exception:  # noqa: BLE001 —— 状态行永远不该拖垮主流程
        return None

    running = [t for t in tasks if getattr(t, "status", None) == TaskStatus.RUNNING]
    if not running:
        return None

    ts = time.monotonic() if now is None else now
    text = " · ".join([f"后台 {len(running)}", *(format_task(t, ts) for t in running)])
    return _clip(text, _resolve_width(width))


__all__ = [
    "build_status_text",
    "disp_width",
    "format_elapsed",
    "format_task",
]
