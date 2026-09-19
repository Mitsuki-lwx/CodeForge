"""会话恢复。

从 conversation.jsonl 恢复对话：坏行跳过、悬空工具调用截断、
token 超限先压缩、时间跨度提醒。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
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

logger = logging.getLogger(__name__)

# 时间跨度提醒阈值（小时）
RECOVERY_STALE_HOURS = 6


@dataclass
class RestoreResult:
    """恢复结果。"""

    conversation: ConversationManager
    skipped: int = 0  # 跳过的坏行数
    compacted: bool = False  # 是否触发过压缩
    time_gap_seconds: float = 0.0  # 距上次活跃时长
    # 发起但从未落盘结果的工具调用（截断掉的那些）。
    # 恢复逻辑据此交叉副作用 journal 判断「哪些调用可能已经生效」（见
    # core/host/recovery.py）——截断本身不再静默丢信息。
    unpaired_tool_uses: list[Message] = field(default_factory=list)


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
        reasoning=data.get("reasoning", ""),
    )


def read_messages(jsonl: Path) -> tuple[list[Message], int, float]:
    """逐行解析；坏行跳过；从最后一个 compact 标记之后开始累积。

    公开而非私有：`codeforge attach` 要读一个**不是当前活跃 run** 的会话，
    那条路径上只有磁盘上的 JSONL，没有内存里的 ConversationManager。
    """
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


def _is_tool_use_msg(m: Message) -> bool:
    """是否是「assistant 发起工具调用」的消息。"""
    return bool(
        m.role == MessageRole.ASSISTANT and m.tool_name and m.tool_use_id
    )


def _unpaired_tool_use_indices(msgs: list[Message]) -> list[int]:
    """返回「发起了 tool_use 但没有配对结果」的消息下标（升序）。

    正常收尾的会话里这个列表必然为空：`Agent` 在每轮结束前会给**每一个**
    tool_use 都补上结果（包括被 deny 和用户拒绝的），所以「未配对」只可能来自
    进程在工具批次中途死掉。

    注意与 `core/agent/fork.py::build_forked` 的区别：那边只反向扫描**尾部**连续
    的 tool_use（fork 只会发生在尾部，且必须跳过历史遗留条目，否则会给旧调用
    补一堆占位结果）。恢复要的是全量语义——宁可多问一句，不能漏掉一个副作用。
    """
    result_ids = {
        m.tool_use_id for m in msgs if m.role == MessageRole.USER and m.tool_use_id
    }
    return [
        i
        for i, m in enumerate(msgs)
        if _is_tool_use_msg(m) and m.tool_use_id not in result_ids
    ]


def read_unpaired_tool_uses(session_dir: str | Path) -> list[Message]:
    """只读地取出「发起过、但从未落盘结果」的工具调用。

    与 `restore_session` 的分工：这里**不**建 `ConversationManager`、不压缩、**不写回
    任何东西**。`codeforge attach` 要的只是"报给用户看"，那条路径绝不能因为看一眼就
    改写别人的会话文件（`restore_session` 在压缩时会把 JSONL 整体重写，见
    `_persist_compact`）。

    恢复语义的入口：把返回值交给 `core.host.recovery.find_pending_confirmations()`，
    就能交叉出「可能已经生效但结果没落盘」的副作用调用。
    """
    msgs, _skipped, _ts = read_messages(
        Path(session_dir) / CONVERSATION_FILENAME
    )
    return [msgs[i] for i in _unpaired_tool_use_indices(msgs)]


def _truncate_dangling_tool_use(msgs: list[Message]) -> list[Message]:
    """把「未配对的工具调用」所在的那一整批调用连同其后内容截掉。

    策略不变（截断而非重放、不伪造结果），但截断点必须退到**整批 tool_use 的起点**。

    落盘顺序是「先所有 tool_use，再所有 tool_result」（`Agent` 在每轮结束前统一
    回灌），所以批次中途崩溃会留下 `[A(t1), A(t2), U(r1)]` 这种半截形状。此时：

    - 截到「最后一个带 tool_use 的消息」（原实现）→ `[A(t1)]`，A(t1) 仍无结果；
    - 截到「第一个未配对消息」（A(t2)）→ `[A(t1)]`，同样留下无结果的 A(t1)；
    - 截到**批次起点**（A(t1) 之前）→ `[]`，干净。

    前两种都会让恢复出来的对话以「assistant 发起 tool_call 却没有对应 tool 结果」
    结尾——模型 API 会直接判为非法请求，恢复出来的会话第一轮就 400。
    """
    bad = _unpaired_tool_use_indices(msgs)
    if not bad:
        return msgs
    cut = bad[0]
    while cut > 0 and _is_tool_use_msg(msgs[cut - 1]):
        cut -= 1
    return msgs[:cut]


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


def _persist_compact(session_dir: Path, msgs: list[Message]) -> None:
    """压缩后把新消息写回 JSONL：先写 compact 标记，再逐条重写。

    下次恢复时 `read_messages` 遇 compact 标记会丢弃旧消息，只读到压缩后的
    内容——避免每次 resume 都重新压缩一次。失败仅告警不阻断恢复（下次仍会压缩，
    语义不变）。
    """
    from core.archive.writer import Writer

    try:
        writer = Writer(session_dir)
        try:
            writer.append_compact_marker()
            for m in msgs:
                writer.append(m)
        finally:
            writer.close()
    except Exception as e:  # noqa: BLE001 —— 写回失败不阻断恢复
        logger.warning("压缩结果写回失败（不影响本次恢复）: %s", e)


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
        RestoreResult(conversation, skipped, compacted, time_gap_seconds,
        unpaired_tool_uses)。
    """
    d = Path(session_dir)
    msgs, skipped, last_ts = read_messages(d / CONVERSATION_FILENAME)
    # 先记录未配对项再截断——截断会把它们从 msgs 里抹掉，而恢复判定要用
    unpaired = [msgs[i] for i in _unpaired_tool_use_indices(msgs)]
    msgs = _truncate_dangling_tool_use(msgs)

    session = _make_session_context(d)
    msgs, compacted = await _maybe_compact(
        msgs, provider_config, context_window, session
    )

    # 压缩后写回 JSONL（先 compact 标记再逐条重写），避免下次恢复重复压缩。
    if compacted:
        _persist_compact(d, msgs)

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
        unpaired_tool_uses=unpaired,
    )
