"""队员 Loop 头部 —— 邮箱未读消息注入与 plan_approval 处理。

在 run_to_completion 每轮调 LLM 前调用 `ingest_team_mailbox(tc)`，若当前处于队员上下文：
- 读未读消息，构造 `<incoming-messages>` system reminder（F42），返回给调用方追加进本轮提醒。
- 收到 `plan_approval_response(approve=True)` → 切权限模式到 default 的提示（F44）。
- mark_read 标记已读。

不直接 import mailbox（避免 agent → team 环），改用 TeammateContext 上注入的闭包。
"""

from __future__ import annotations

from typing import Any

from core.agent.team_hook import IncomingMessage, TeammateContext


def build_incoming_reminder(unread: list[IncomingMessage]) -> str:
    """构造 `<incoming-messages>` reminder 文本（F42）。"""
    lines = [f"<incoming-messages>\n收到 {len(unread)} 条新消息:"]
    for i, m in enumerate(unread, 1):
        ts = _fmt_ts(m) or ""
        content_preview = m.content[:200]
        lines.append(
            f"[{i}] 来自 {m.from_}(type={m.type},{ts}): {m.summary}\n    {content_preview}"
        )
    lines.append("</incoming-messages>")
    return "\n".join(lines)


def _fmt_ts(m: IncomingMessage) -> str:
    # payload 里可能带时间戳；没有则留空。
    if m.payload and isinstance(m.payload, dict):
        ts = m.payload.get("ts") or m.payload.get("timestamp")
        if ts:
            return f"ts={ts}"
    return ""


async def ingest_team_mailbox(
    tc: TeammateContext,
    append_reminder: Any = None,
) -> list[str]:
    """读队员未读消息，返回待追加的 reminder 文本列表，并 mark_read。

    Args:
        tc: 当前 TeammateContext。
        append_reminder: 可选的可调用对象，把生成在 pending_reminders 上。

    Returns:
        本轮回应追加的 reminder 列表（调用方决定注入方式）。
    """
    indices, msgs = await tc.read_unread()
    if not msgs:
        return []

    reminders: list[str] = [build_incoming_reminder(msgs)]
    # 处理 plan_approval_response
    for m in msgs:
        if m.type == "plan_approval_response" and m.payload:
            approved = m.payload.get("approve")
            if approved is True:
                reminders.append(
                    "Lead 已批准计划，权限模式已切到 default，可执行计划。"
                )
            else:
                feedback = m.payload.get("feedback", "")
                reminders.append(
                    f"Lead 驳回了计划，反馈：{feedback}。请调整后重新提交。"
                )

    await tc.mark_read(indices)

    if append_reminder is not None:
        for r in reminders:
            append_reminder(r)
    return reminders


__all__ = ["build_incoming_reminder", "ingest_team_mailbox"]
