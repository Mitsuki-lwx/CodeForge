"""后台任务 / 队友的列表与 transcript 渲染（**纯函数**）。

无 IO、无 TUI 依赖，可脱离终端单测。

与状态行（`tui/teammate_status.py`）**共用同一份**显示名 / 耗时 / 宽度截断实现 ——
上一轮真链路已经证明「显示名取值顺序」是个坑（`AgentTool.name` 常常为空，
状态行一度只剩 hex），**不能再有第二份规则**。

动机与形态选择：`docs/spec_teammate_inspect.md` §3、§4。
"""

from __future__ import annotations

import json
import time
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from core.task.manager import BackgroundTask, TaskStatus

# 显示名 / 段落的显示上限（**列**，中文算 2 列）
NAME_MAX_COLS = 20
LIST_NAME_MAX_COLS = 26
ACTIVITY_MAX_COLS = 12
STATUS_MAX_COLS = 8
ELAPSED_MAX_COLS = 6
# 工具参数单行展示上限（列）
TOOL_ARGS_MAX_COLS = 120
TOOL_ARG_VALUE_MAX_COLS = 40
# transcript 单条消息默认截断（字符）
MESSAGE_MAX_CHARS = 1000
# transcript 默认显示条数
DEFAULT_TAIL = 20

_STATUS_LABELS: dict[Any, str] = {
    TaskStatus.RUNNING: "运行中",
    TaskStatus.COMPLETED: "已完成",
    TaskStatus.FAILED: "失败",
    TaskStatus.CANCELLED: "已取消",
}


# ── 宽度/截断（状态行共用）────────────────────────────────────────


def _cols(ch: str) -> int:
    """单字符显示宽度：东亚全角算 2 列。"""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def disp_width(text: str) -> int:
    """字符串显示宽度（列）。中英混排时不能直接用 `len`。"""
    return sum(_cols(c) for c in text)


def clip(text: str, max_cols: int) -> str:
    """按**显示宽度**截断并加省略号。"""
    if max_cols <= 1:
        return ""
    if disp_width(text) <= max_cols:
        return text
    out: list[str] = []
    used = 0
    for ch in text:
        w = _cols(ch)
        if used + w > max_cols - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def pad(text: str, cols: int) -> str:
    """按**显示宽度**右侧补空格（列对齐用；中文占 2 列）。"""
    return text + " " * max(0, cols - disp_width(text))


def format_elapsed(seconds: float) -> str:
    """耗时：`12s` / `1m23s`。"""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m{s % 60:02d}s"


def display_name(bt: Any, max_cols: int = NAME_MAX_COLS) -> str:
    """显示名：`name` → 任务文本首行 → id 短码。

    ★ 实测（真链路）：`AgentTool` 的 `name` 是**可选**参数，后台子 Agent 常常
    没有名字，于是只剩一串 hex（`12def532`）—— 对"看得见在干什么"毫无帮助。
    所以退回**任务文本首行**，它天然说明了这个任务在做什么。
    """
    name = str(getattr(bt, "name", "") or "").strip()
    if name:
        return clip(name, max_cols)

    for line in str(getattr(bt, "task", "") or "").splitlines():
        head = line.strip()
        if head:
            return clip(head, max_cols)

    tid = str(getattr(bt, "id", "") or "").removeprefix("task_")
    return clip(tid[:8], max_cols) or "?"


def status_label(status: Any) -> str:
    """任务状态的中文文案。"""
    return _STATUS_LABELS.get(status, str(getattr(status, "name", status)))


# ── 列表 ─────────────────────────────────────────────────────────


def agent_line(index: int, bt: Any, now: float, *, queued: int = 0) -> str:
    """列表里的一行：`  #3  alice  运行中  1m23s  8步  Grep  排队 1 条`。

    步数为 0 / 最近动作为空 / 无排队消息时**省略对应段**（不显示噪音）。
    """
    start = float(getattr(bt, "start_time", 0.0) or 0.0)
    end = float(getattr(bt, "end_time", 0.0) or 0.0)
    steps = int(getattr(bt, "tool_count", 0) or 0)
    activity = str(getattr(bt, "last_activity", "") or "").strip()

    head = (
        f"  #{pad(str(index), 4)}"
        f"{pad(display_name(bt, LIST_NAME_MAX_COLS), LIST_NAME_MAX_COLS + 1)}"
        f"{pad(status_label(getattr(bt, 'status', None)), STATUS_MAX_COLS)}"
        f"{pad(format_elapsed((end or now) - start), ELAPSED_MAX_COLS)}"
    )
    tail = "  ".join(
        x
        for x in (
            f"{steps}步" if steps > 0 else "",
            clip(activity, ACTIVITY_MAX_COLS) if activity else "",
            f"排队 {queued} 条" if queued > 0 else "",
        )
        if x
    )
    return (head + ("  " + tail if tail else "")).rstrip()


def _visible_rows(
    indexed: list[tuple[int, Any]], *, show_all: bool, finished_limit: int
) -> tuple[list[tuple[int, Any]], int]:
    """挑出要显示的行 + 隐藏数量。

    默认 = **全部 RUNNING + 最近 `finished_limit` 个已结束**（`manager._tasks`
    没有淘汰，长会话会攒下几十个已完成任务）。显示顺序仍按序号升序 ——
    **刻意不做"运行中优先"重排**：状态会变，重排会让"序号 N"立刻指向别的任务。
    """
    if show_all:
        return indexed, 0
    finished = [
        (i, t) for i, t in indexed if getattr(t, "status", None) != TaskStatus.RUNNING
    ]
    keep = {
        id(t) for _, t in (finished[-finished_limit:] if finished_limit > 0 else [])
    }
    visible = [
        (i, t)
        for i, t in indexed
        if getattr(t, "status", None) == TaskStatus.RUNNING or id(t) in keep
    ]
    return visible, len(indexed) - len(visible)


def render_agent_list(
    tasks: Iterable[Any],
    *,
    now: float | None = None,
    show_all: bool = False,
    finished_limit: int = 5,
    queued: Mapping[str, int] | None = None,
) -> list[str]:
    """渲染 `/agents` 的列表（含表头与用法提示）。

    序号 = 传入顺序里的位置（调用方保证是 `task_mgr.list()`，按 `start_time` 升序）
    —— 只增不改，同一会话内稳定可复现。
    """
    items = list(tasks)
    if not items:
        return ["没有后台任务。"]

    ts = time.monotonic() if now is None else now
    indexed = list(enumerate(items, start=1))
    running = sum(
        1 for _, t in indexed if getattr(t, "status", None) == TaskStatus.RUNNING
    )
    q = queued or {}

    visible, hidden = _visible_rows(
        indexed, show_all=show_all, finished_limit=finished_limit
    )
    lines = [f"后台任务 {len(items)}（运行中 {running}）"]
    lines.extend(
        agent_line(i, t, ts, queued=int(q.get(str(getattr(t, "id", "")), 0) or 0))
        for i, t in visible
    )
    if hidden:
        lines.append(f"  另有 {hidden} 个更早的任务未显示：/agents all")
    lines.append("  /agents show <序号|id|名字> · tell <…> <消息> · stop <…>")
    return lines


# ── transcript ───────────────────────────────────────────────────

_SEP = "─" * 56
_LABEL_COLS = 8


def _block(label: str, text: str) -> list[str]:
    """一条消息渲染成多行：首行带标签，续行按标签宽度缩进。"""
    rows = text.splitlines() or [""]
    out = [f"  {pad(label, _LABEL_COLS)}: {rows[0]}"]
    out.extend(f"  {' ' * _LABEL_COLS}: {r}" for r in rows[1:])
    return out


def _clip_message(text: str, *, max_chars: int, full: bool) -> str:
    if full or max_chars <= 0 or len(text) <= max_chars:
        return text
    return (
        text[:max_chars]
        + f"…（共 {len(text)} 字符，显示前 {max_chars}；--full 看全文）"
    )


def _compact_args(tool_input: Any) -> str:
    """工具参数压成单行 `k=v, k=v`（值过长截断）。"""
    if not isinstance(tool_input, dict) or not tool_input:
        return ""
    parts: list[str] = []
    for k, v in tool_input.items():
        if isinstance(v, str):
            s = f"'{' '.join(v.split())}'"
        else:
            s = json.dumps(v, ensure_ascii=False, default=str)
        parts.append(f"{k}={clip(s, TOOL_ARG_VALUE_MAX_COLS)}")
    return clip(", ".join(parts), TOOL_ARGS_MAX_COLS)


def message_lines(
    m: Any, *, max_chars: int = MESSAGE_MAX_CHARS, full: bool = False
) -> list[str]:
    """把一条 `Message` 渲染成展示行（无法识别的返回空列表）。"""
    tool_name = str(getattr(m, "tool_name", "") or "")
    if tool_name:
        args = _compact_args(getattr(m, "tool_input", None))
        return _block("tool", f"{tool_name}({args})")

    text = str(getattr(m, "content", "") or "")

    if getattr(m, "tool_use_id", None):
        return _block("result", _clip_message(text, max_chars=max_chars, full=full))

    if text.startswith("[system_reminder]"):
        inner = text[len("[system_reminder]") :]
        inner = inner.removesuffix("[/system_reminder]").strip()
        return _block("sys", _clip_message(inner, max_chars=max_chars, full=full))

    if not text.strip():
        return []

    role = getattr(m, "role", None)
    is_user = str(getattr(role, "value", role)) == "user"
    return _block(
        "You" if is_user else "Agent",
        _clip_message(text, max_chars=max_chars, full=full),
    )


def render_transcript(
    bt: Any,
    *,
    index: int = 0,
    now: float | None = None,
    tail: int = DEFAULT_TAIL,
    full: bool = False,
    max_chars: int = MESSAGE_MAX_CHARS,
) -> list[str]:
    """渲染一个任务的 transcript（头 + 消息 + 尾注）。

    头部始终打印任务摘要 —— "这条 transcript 是谁的"永远清楚。
    """
    ts = time.monotonic() if now is None else now
    start = float(getattr(bt, "start_time", 0.0) or 0.0)
    end = float(getattr(bt, "end_time", 0.0) or 0.0)
    steps = int(getattr(bt, "tool_count", 0) or 0)

    head = f"#{index}  " if index > 0 else ""
    head += (
        f"{display_name(bt, LIST_NAME_MAX_COLS)}  ·  "
        f"{status_label(getattr(bt, 'status', None))}  "
        f"{format_elapsed((end or ts) - start)}"
        f"{f'  {steps}步' if steps else ''}"
    )
    task_text = " ".join(str(getattr(bt, "task", "") or "").split())
    if task_text:
        head += f"  ·  任务：{clip(task_text, 60)}"

    msgs = list(getattr(getattr(bt, "conv", None), "messages", None) or [])
    total = len(msgs)
    shown = msgs[-tail:] if tail and tail > 0 else msgs

    lines = [head, _SEP]
    if not shown:
        lines.append("  （还没有消息）")
    for m in shown:
        lines.extend(message_lines(m, max_chars=max_chars, full=full))

    if total > len(shown):
        lines.append(
            f"  （共 {total} 条消息，显示最近 {len(shown)} 条；"
            f"调整：/agents show {index or getattr(bt, 'id', '')} --tail {total}）"
        )
    else:
        lines.append(f"  （共 {total} 条消息）")
    return lines


# ── 选择器 ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class Resolved:
    """选择器解析结果：`ok` 为假时 `error` 是给人看的原因。"""

    index: int = 0
    task: Any = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.task is not None


_USAGE = "缺少选择器（用法：/agents show <序号|id|名字>）"


def _ambiguous(rows: list[tuple[int, Any]], sel: str) -> Resolved:
    cands = "、".join(f"#{i} {display_name(t)}" for i, t in rows[:5])
    return Resolved(error=f"'{sel}' 有歧义，匹配到 {len(rows)} 个：{cands}")


def resolve_selector(
    sel: str, tasks: Iterable[Any], *, usage: str = _USAGE
) -> Resolved:
    """把 `/agents` 的选择器解析成任务。

    支持：1 起始的**序号** → 完整 id → id 前缀 → 名字。未找到与歧义都返回明确原因。
    """
    items = list(tasks)
    s = str(sel or "").strip()
    if not s:
        return Resolved(error=usage)

    if s.isdigit():
        n = int(s)
        if 1 <= n <= len(items):
            return Resolved(index=n, task=items[n - 1])
        return Resolved(error=f"序号超出范围：{n}（共 {len(items)} 个任务）")

    indexed = list(enumerate(items, start=1))
    exact = [(i, t) for i, t in indexed if str(getattr(t, "id", "")) == s]
    if exact:
        return Resolved(index=exact[0][0], task=exact[0][1])

    if s.startswith("task_"):
        pref = [(i, t) for i, t in indexed if str(getattr(t, "id", "")).startswith(s)]
        if len(pref) == 1:
            return Resolved(index=pref[0][0], task=pref[0][1])
        if len(pref) > 1:
            return _ambiguous(pref, s)

    by_name = [(i, t) for i, t in indexed if str(getattr(t, "name", "") or "") == s]
    if len(by_name) == 1:
        return Resolved(index=by_name[0][0], task=by_name[0][1])
    if len(by_name) > 1:
        return _ambiguous(by_name, s)

    return Resolved(error=f"未找到任务 '{s}'（可用序号 / id / 名字，/agents 看列表）")


__all__ = [
    "DEFAULT_TAIL",
    "MESSAGE_MAX_CHARS",
    "BackgroundTask",
    "Resolved",
    "agent_line",
    "clip",
    "disp_width",
    "display_name",
    "format_elapsed",
    "message_lines",
    "pad",
    "render_agent_list",
    "render_transcript",
    "resolve_selector",
    "status_label",
]
