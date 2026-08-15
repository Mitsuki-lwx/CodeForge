"""TUI Lead 邮箱 watcher —— 队员通知 Lead 的关键路径（F41a/F41b）。

CodeForge 无 tui/tasks.py；这些 helper 独立于此模块，供 MewCodeApp 在 on_mount 启动。
- build_team_update_reminder: 把 LeadMessage 列表渲染成 `<team-update>` reminder（content 截断 8000）。
- lead_mail_message / wait_for_lead_mail: 后台 task 阻塞于 lead_mail_event，收到信号转给 App。
- begin_autonomous_turn: Lead idle 时合成一条 user 消息自动开新轮。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable

# content 透传上限（允许队员完整报告透传）。
TEAM_UPDATE_TRUNCATE = 8000


def build_team_update_reminder(msgs: Iterable[dict]) -> str:
    """构建 `<team-update>` reminder 文本。"""
    msgs = list(msgs)
    if not msgs:
        return ""
    lines = [f"<team-update> 队员发来 {len(msgs)} 条更新:"]
    for i, m in enumerate(msgs, 1):
        preview = _truncate(m.get("content", ""), TEAM_UPDATE_TRUNCATE)
        lines.append(
            f"[{i}] team={m.get('team_name')} from={m.get('from_')} "
            f"(type={m.get('type')}):\n{preview}"
        )
    lines.append("</team-update>")
    return "\n".join(lines)


def _truncate(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[:limit] + "…[截断]"


async def wait_for_lead_mail(
    event: Awaitable,
    post_message: Callable[[object], None],
    message_cls: type,
) -> None:
    """阻塞于 lead_mail_event；收到信号后清旗标并投递给 App handler。

    由 on_mount 启动并注册到 app 生命周期；每轮处理后 App 需重新 create_task 续接。
    """
    await _wait(event)

    # 清旗标，让后续信号也能触发
    _clear(event)
    post_message(message_cls())


async def _wait(event: Awaitable) -> None:
    import asyncio

    if isinstance(event, asyncio.Event):
        await event.wait()
    else:
        await event  # 允许传入已完成 Awaitable（测试桩）


def _clear(event: Awaitable) -> None:
    import asyncio

    if isinstance(event, asyncio.Event):
        event.clear()


def begin_autonomous_turn_text() -> str:
    """Lead idle 自动续推合成的 user 消息文本。"""
    return "[team-update] 队员发来新消息，请按 Coordinator 流程处理..."


__all__ = [
    "TEAM_UPDATE_TRUNCATE",
    "begin_autonomous_turn_text",
    "build_team_update_reminder",
    "wait_for_lead_mail",
]
