"""会话恢复。

从 conversation.jsonl 恢复对话：坏行跳过、悬空工具调用截断、
token 超限先压缩、时间跨度提醒。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from conversation.manager import ConversationManager
from conversation.message import Message, MessageRole, MessageStatus
from core.archive.writer import CONVERSATION_FILENAME
from core.context_compression import ManageInput, TriggerKind, manage_context
from core.context_compression.const import AUTO_SAFETY_MARGIN, SUMMARY_RESERVE
from core.context_compression.state import (
    CompactCircuitBreaker,
    ContentReplacementState,
    RecoveryState,
    SessionContext,
)
from core.context_compression.token import estimate_tokens

# 时间跨度提醒阈值（小时）
RECOVERY_STALE_HOURS = 6


@dataclass
class RestoreResult:
    """恢复结果。"""

    conversation: ConversationManager
    skipped: int = 0  # 跳过的坏行数
    compacted: bool = False  # 是否触发过压缩
    time_gap_seconds: float = 0.0  # 距上次活跃时长


def _deserialize(data: dict) -> Message:
    """反序列化 JSON 行 → Message；字段缺失用安全默认值。"""
    role = MessageRole(data["role"])  # role 必需，非法则抛 ValueError（作为坏行）
    status_raw = data.get("status")
    status = (
        MessageStatus(status_raw)
        if status_raw and status_raw in MessageStatus._value2member_map_
        else MessageStatus.COMPLETED
    )
    return Message(
        role=role,
        content=data.get("content", ""),
        id=data.get("id"),
        status=status,
        timestamp=data.get("timestamp"),
        usage=data.get("usage"),
        tool_use_id=data.get("tool_use_id"),
        tool_name=data.get("tool_name"),
        tool_input=data.get("tool_input"),
    )


def _read_messages(jsonl: Path) -> tuple[list[Message], int, float]:
    """逐行解析；坏行跳过；从最后一个 compact 标记之后开始累积。"""
    msgs: list[Message] = []
    skipped = 0
    last_ts = 0.0
    if not jsonl.exists():
        return msgs, skipped, last_ts

    for line in jsonl.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if data.get("type") == "compact":
            msgs = []
            last_ts = 0.0
            continue
        try:
            msg = _deserialize(data)
        except (ValueError, KeyError, TypeError):
            skipped += 1
            continue
        msgs.append(msg)
        ts = data.get("ts")
        if isinstance(ts, (int, float)):
            last_ts = float(ts)
    return msgs, skipped, last_ts


def _truncate_dangling_tool_use(msgs: list[Message]) -> list[Message]:
    """末尾 assistant 工具调用无配对结果时，截断到该条之前。"""
    if not msgs:
        return msgs
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if m.role == MessageRole.ASSISTANT and m.tool_name and m.tool_use_id:
            has_result = any(
                mm.role == MessageRole.USER and mm.tool_use_id == m.tool_use_id
                for mm in msgs[i + 1 :]
            )
            return msgs[:i] if not has_result else msgs
        if m.role == MessageRole.USER and not m.tool_use_id:
            break
    return msgs


def _humanize_duration(gap_seconds: float) -> str:
    mins = int(gap_seconds // 60)
    if mins < 60:
        return f"{max(1, mins)} 分钟"
    hours = mins // 60
    if hours < 24:
        return f"{hours} 小时"
    return f"{hours // 24} 天"


def _time_reminder(gap_seconds: float) -> str:
    return (
        f"[系统提示] 本会话已暂停 {_humanize_duration(gap_seconds)}。"
        "部分上下文可能已过时，如需最新信息请重新读取相关文件。"
    )


def _make_session_context(session_dir: Path) -> SessionContext:
    return SessionContext(
        session_id=session_dir.name,
        session_dir=str(session_dir),
        spill_dir=str(session_dir / "tool-results"),
    )


async def _maybe_compact(
    msgs: list[Message],
    provider_config,
    context_window: int,
    session: SessionContext,
) -> tuple[list[Message], bool]:
    """估算超阈值时先执行一次压缩；失败降级为原文。"""
    threshold = context_window - SUMMARY_RESERVE - AUTO_SAFETY_MARGIN
    if not msgs or estimate_tokens(0, msgs, 0) <= threshold:
        return msgs, False

    conv = ConversationManager()
    conv.replace_history(msgs)
    in_ = ManageInput(
        conv=conv,
        provider_config=provider_config,
        model=provider_config.model,
        context_window=context_window,
        tool_defs=[],
        replacement=ContentReplacementState(),
        recovery=RecoveryState(),
        auto_tracking=CompactCircuitBreaker(),
        session=session,
        usage_anchor=0,
        anchor_msg_len=0,
        estimated_token=estimate_tokens(0, msgs, 0),
        trigger=TriggerKind.AUTO,
    )
    try:
        await manage_context(in_)
    except Exception:  # noqa: BLE001 —— 压缩失败降级为原文恢复
        return msgs, False
    return conv.messages, True


async def restore_session(
    session_dir: str | Path,
    *,
    provider_config,
    context_window: int,
) -> RestoreResult:
    """从 JSONL 恢复会话为可用的 ConversationManager。

    Args:
        session_dir: 会话目录（含 conversation.jsonl）。
        provider_config: 用于超限压缩的 provider 配置。
        context_window: 上下文窗口，用于超限阈值判断。

    Returns:
        RestoreResult(conversation, skipped, compacted, time_gap_seconds)。
    """
    d = Path(session_dir)
    msgs, skipped, last_ts = _read_messages(d / CONVERSATION_FILENAME)
    msgs = _truncate_dangling_tool_use(msgs)

    session = _make_session_context(d)
    msgs, compacted = await _maybe_compact(
        msgs, provider_config, context_window, session
    )

    gap = 0.0
    if last_ts:
        gap = time.time() - last_ts
        if gap > RECOVERY_STALE_HOURS * 3600:
            msgs.append(
                Message(
                    role=MessageRole.USER,
                    content=_time_reminder(gap),
                    status=MessageStatus.COMPLETED,
                )
            )

    conv = ConversationManager()
    conv.replace_history(msgs)
    return RestoreResult(
        conversation=conv,
        skipped=skipped,
        compacted=compacted,
        time_gap_seconds=gap,
    )
