"""终端内嵌 HITL 确认组件 —— 方向键 + 回车选择。

复用 tui.select 的裸键选择器，不依赖 prompt_toolkit。
此前在 agent 事件循环中途 re-entrant 调用 session.prompt_async
会导致终端状态错乱而卡死，改用裸键读取根治。
"""

from __future__ import annotations

from rich.console import Console

from core.permissions.hitl import HITLChoice, HITLRequest, HITLResponse
from tui.select import select_from_options

_OPTIONS = [
    (HITLChoice.ALLOW_ONCE, "Allow this time"),
    (HITLChoice.ALLOW_SESSION, "Allow this session"),
    (HITLChoice.DENY, "Deny"),
]


def show_hitl_dialog(
    console: Console,
    request: HITLRequest,
) -> HITLResponse:
    """显示 HITL 确认选择框并返回用户选择。

    Esc / Ctrl+C 视为拒绝。
    """
    title = f"⚠ Permission Required — {request.tool_name}"
    subtitle_parts = [request.description or ""]
    if request.risk_hint:
        subtitle_parts.append(f"Risk: {request.risk_hint}")
    subtitle = "  |  ".join(p for p in subtitle_parts if p)

    index = select_from_options(
        console,
        title=title,
        subtitle=subtitle,
        options=[label for _choice, label in _OPTIONS],
        hint="↑/↓ 选择，Enter 确认，Esc 拒绝",
        cancel_index=2,  # 拒绝
    )
    choice = _OPTIONS[index][0]
    return HITLResponse(choice=choice, tool_name=request.tool_name)
