"""`/show` 的 TUI 粘合层。

渲染是纯函数（`core/task/view.py`）；这里只做：取主会话消息 → 算用量 → 调渲染。

与 `tui/agent_view.py` 的分工完全一致（那里是"队友"，这里是"主会话"）：
**这里不含任何执行逻辑、不加 await、不碰收尾路径**
（`docs/spec_show_redact.md` §3 的零回归要求）。

为什么是命令式而不是 `Ctrl+O` 式就地展开：见 `docs/ref_omp_secrets.md` §1.2
与 `docs/spec_show_redact.md` §4.1 —— 阻塞式读键会冻住事件循环（已实测）。
"""

from __future__ import annotations

import logging
from typing import Any

from core.task.view import (
    DEFAULT_TAIL,
    render_cost_summary,
    render_round_overview,
    render_session_transcript,
    split_rounds,
)

logger = logging.getLogger(__name__)

#: 概览默认显示的轮数（比 transcript 的 20 条消息更宽松 —— 一行就能代表一轮）
DEFAULT_ROUND_TAIL = 10


def messages_of(app: Any) -> list:
    """主会话消息列表。拿不到就返回空（不抛 —— 下钻不该拖垮主流程）。"""
    conv = getattr(app, "conversation", None)
    if conv is None:
        agent = getattr(app, "agent", None)
        conv = getattr(agent, "_conversation", None)
    if conv is None:
        return []
    try:
        return list(conv.messages)
    except Exception as e:  # noqa: BLE001
        logger.debug("cannot read conversation messages from %r: %s", type(app), e)
        return []


def _usage(app: Any) -> tuple[int, int]:
    """本会话 input/output token（复用 `/status` 同一份口径）。"""
    try:
        agent = getattr(app, "agent", None)
        u = getattr(agent, "_total_usage", None) or {}
        return int(u.get("input_tokens", 0) or 0), int(u.get("output_tokens", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0, 0


def _tool_calls(app: Any) -> int:
    """本会话工具调用数：UI 口径（`tool_count` 是**已启用**的工具数，不是调用数）。

    所以这里从 trace 计数 —— 无可观测性时退化为按消息里的 `tool_name` 数。
    """
    try:
        from core.trace.reader import session_summary

        s = session_summary(app.session_id())
        n = int(s.get("tools", 0) or 0)
        if n:
            return n
    except Exception as e:  # noqa: BLE001 —— 降级到消息计数，但要留下痕迹
        logger.debug("tool count from trace unavailable, fall back to messages: %s", e)
    return sum(1 for m in messages_of(app) if getattr(m, "tool_name", ""))


def _redaction_on() -> bool:
    try:
        from core.observability.redact import redact_config_lazy

        return redact_config_lazy()
    except Exception:  # noqa: BLE001
        return True


def overview(app: Any, *, tail: int = DEFAULT_ROUND_TAIL) -> list[str]:
    """`/show`：轮次概览。`tail=0` 表示全部。"""
    return render_round_overview(messages_of(app), tail=tail)


def transcript(
    app: Any, index: int, *, tail: int = DEFAULT_TAIL, full: bool = False,
    only_tools: bool = False,
) -> list[str]:
    """`/show <n>`：某一轮全文。"""
    msgs = messages_of(app)
    total = len(split_rounds(msgs))
    hint = f"/show --tail {total} --full" if total else ""
    return render_session_transcript(
        msgs, index=index, tail=tail, full=full, only_tools=only_tools, hint_more=hint
    )


def cost(app: Any) -> list[str]:
    """`/show --cost`：本会话用量汇总。"""
    t_in, t_out = _usage(app)
    return render_cost_summary(
        tokens_in=t_in,
        tokens_out=t_out,
        tool_calls=_tool_calls(app),
        redaction_on=_redaction_on(),
    )


def render(app: Any, args: str) -> list[str]:
    """`/show` 的统一入口：解析参数 → 选形态。返回展示行（`x ` 开头 = 错误）。"""
    sel, tail, full, only_tools, want_cost = parse_show_args(args)

    if want_cost:
        return cost(app)
    if not sel:
        # ★ tail 要传下去：0 = 全部；未指定时给概览的默认轮数
        return overview(app, tail=tail if tail else DEFAULT_ROUND_TAIL)
    if not sel.isdigit():
        return [f"x 轮次号必须是正整数，收到 '{sel}'（/show 看列表）"]

    return transcript(app, int(sel), tail=tail, full=full, only_tools=only_tools)


def parse_show_args(args: str) -> tuple[str, int, bool, bool, bool]:
    """解析 `/show` 参数 → `(轮次, tail, full, only_tools, want_cost)`。

    规则与 `/agents show` 一致：选择器取**第一个非选项** token，其余位置无关
    （`--full 3` 也认）。
    """
    sel = ""
    tail = 0  # 0 = 全部（单轮渲染没有"再截断"的必要）
    full = False
    only_tools = False
    want_cost = False

    toks = args.split()
    i = 0
    while i < len(toks):
        tok = toks[i]
        if tok == "--full":
            full = True
        elif tok == "--tools":
            only_tools = True
        elif tok == "--cost":
            want_cost = True
        elif tok == "--tail":
            i += 1
            if i < len(toks):
                tail = _positive_int(toks[i], 0)
        elif tok.startswith("--tail="):
            tail = _positive_int(tok.split("=", 1)[1], 0)
        elif not sel and not tok.startswith("-"):
            sel = tok
        i += 1
    return sel, tail, full, only_tools, want_cost


def _positive_int(raw: str, default: int) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


__all__ = [
    "DEFAULT_ROUND_TAIL",
    "cost",
    "messages_of",
    "overview",
    "parse_show_args",
    "render",
    "transcript",
]
