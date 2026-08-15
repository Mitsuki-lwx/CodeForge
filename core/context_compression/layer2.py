"""第二层兜底压缩。

LLM 摘要生成、PTL 自重试、近期原文边界计算、auto/force_compact。
"""

from __future__ import annotations

import logging
import math
from typing import Any

from conversation.message import (
    Message,
    MessageRole,
    MessageStatus,
)
from core.context_compression.const import (
    ESTIMATE_CHARS_PER_TOKEN,
    PTL_DROP_PERCENTAGE,
    PTL_RETRY_LIMIT,
    RECENT_KEEP_MESSAGES,
    RECENT_KEEP_TOKENS,
)
from core.context_compression.recovery import build_recovery_attachment
from core.context_compression.state import FileReadRecord
from core.context_compression.summary_prompt import (
    build_summary_prompt,
    extract_summary,
)
from core.context_compression.token import estimate_tokens

logger = logging.getLogger(__name__)


# ── 近期原文边界 ──────────────────────────────────────────────────


def pick_recent_tail(msgs: list[Message]) -> list[Message]:
    """从 msgs 尾部累加，同时满足两个下界后停手。

    - 累计估算 token ≥ RECENT_KEEP_TOKENS 且
    - 累计消息数 ≥ RECENT_KEEP_MESSAGES
    （两个下界都满足才停手——择宽保留）

    再做 tool_use/tool_result 配对修正：若截断点夹在配对中间，
    向前推到 tool_use 之前。

    Args:
        msgs: 消息列表。

    Returns:
        尾部近期原文（浅拷贝）。
    """
    if not msgs:
        return []

    accumulated_tokens = 0
    start_idx = len(msgs)

    for i in range(len(msgs) - 1, -1, -1):
        accumulated_tokens += math.ceil(
            _single_msg_chars(msgs[i]) / ESTIMATE_CHARS_PER_TOKEN
        )
        start_idx = i
        # 从尾部累计的条数
        if (
            accumulated_tokens >= RECENT_KEEP_TOKENS
            and (len(msgs) - i) >= RECENT_KEEP_MESSAGES
        ):
            break

    # 配对修正：若 start_idx 落在 tool_result，前推到配对 tool_use 之前
    start_idx = _fix_pair_boundary(msgs, start_idx)

    return list(msgs[start_idx:])


def _single_msg_chars(msg: Message) -> int:
    """计算单条消息的字符数。"""
    content = msg.content
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    # content block list
    chars = 0
    import json

    for block in content:
        if isinstance(block, dict):
            if block.get("type") == "text":
                chars += len(block.get("text", "").encode("utf-8"))
            elif block.get("type") == "tool_use":
                chars += len(
                    json.dumps(block.get("input", {}), ensure_ascii=False).encode(
                        "utf-8"
                    )
                )
            elif block.get("type") == "tool_result":
                rc = block.get("content", "")
                if isinstance(rc, str):
                    chars += len(rc.encode("utf-8"))
    return chars


def _fix_pair_boundary(msgs: list[Message], start_idx: int) -> int:
    """若截断点落在孤立的 tool_result，前推到配对 tool_use 之前。

    检查 msgs[start_idx].role 是否为包含 tool_result 的 user 消息。
    若是且前面有带 tool_use 的 assistant，则前推。

    Args:
        msgs: 消息列表。
        start_idx: 当前截断起始索引。

    Returns:
        修正后的起始索引。
    """
    if start_idx >= len(msgs):
        return start_idx

    first = msgs[start_idx]
    # 检查首条是否为包含 tool_result 的 user 消息
    if first.role == MessageRole.USER and isinstance(first.content, list):
        has_tool_result = any(
            isinstance(b, dict) and b.get("type") == "tool_result"
            for b in first.content
        )
        if has_tool_result:
            # 向前找配对的 assistant(tool_use)
            for j in range(start_idx - 1, -1, -1):
                prev = msgs[j]
                if prev.role == MessageRole.ASSISTANT and isinstance(
                    prev.content, list
                ):
                    has_tool_use = any(
                        isinstance(b, dict) and b.get("type") == "tool_use"
                        for b in prev.content
                    )
                    if has_tool_use:
                        return j
            # 找不到配对 assistant，至少前推到 user 之前
            for j in range(start_idx - 1, -1, -1):
                if msgs[j].role == MessageRole.USER:
                    return j
    return start_idx


def _join_after_summary(
    summary_and_recovery: Message,
    recent: list[Message],
) -> list[Message]:
    """拼接摘要消息和近期原文，处理 role 衔接。

    摘要消息 role 为 user。
    若 recent 首条也是 user → 插入 assistant 占位。
    若 recent 首条是 tool → 前推修正（防御性）。

    Args:
        summary_and_recovery: 合并了摘要和恢复段的单条 user 消息。
        recent: 近期原文列表。

    Returns:
        拼接后的完整消息列表。
    """
    if not recent:
        return [summary_and_recovery]

    result = [summary_and_recovery]

    if recent[0].role == MessageRole.USER:
        # 插入衔接占位 assistant 消息
        result.append(
            Message(
                role=MessageRole.ASSISTANT,
                content="（已加载上下文摘要与恢复信息。请继续。）",
                status=MessageStatus.COMPLETED,
            )
        )
    elif recent[0].role != MessageRole.ASSISTANT:
        # 防御性：如果不是 assistant 也不是 user 开头
        # 检查是否为 role=user 的 tool_result（配对修正应已处理）
        pass

    result.extend(recent)
    return result


# ── 分组 ──────────────────────────────────────────────────────────


def _is_tool_result_msg(m: Message) -> bool:
    """判断一条 user 消息是否为 tool_result 而非真正的用户提交。"""
    if m.tool_use_id is not None:
        return True
    if isinstance(m.content, list):
        return any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in m.content
        )
    return False


def group_by_user_turn(msgs: list[Message]) -> list[list[Message]]:
    """按 "用户提交 → 一组 assistant/tool 往返" 分组。

    只有真正的用户文本消息（非 tool_result）才开新组。
    第一条不是 user 时单独塞进第 0 组。

    Args:
        msgs: 消息列表。

    Returns:
        二维列表，每组是连续的消息子列表。
    """
    if not msgs:
        return []

    groups: list[list[Message]] = []
    current: list[Message] = []

    for m in msgs:
        if m.role == MessageRole.USER and not _is_tool_result_msg(m) and current:
            groups.append(current)
            current = []
        current.append(m)

    if current:
        groups.append(current)

    return groups


# ── 摘要请求 ──────────────────────────────────────────────────────


async def summarize_once(
    provider_config: Any,
    model: str,
    msgs: list[Message],
) -> str:
    """发一次摘要请求。

    摘要请求 tools=None（不传工具定义），防止模型在摘要阶段调用工具。
    返回 extract_summary 处理后的文本。
    异常透传——PromptTooLongError 由调用方 isinstance 判断。

    Args:
        provider_config: ProviderConfig 实例。
        model: 模型名称。
        msgs: 用于构建摘要 Prompt 的消息。

    Returns:
        解析后的摘要文本。

    Raises:
        PromptTooLongError: 上下文过长。
        Exception: 其他模型调用异常。
    """
    from llm.client import LLMClient

    client = LLMClient.create(provider_config)
    prompt_msgs = build_summary_prompt(msgs)

    # 转为 APIMessage
    api_msgs = [m.to_api() for m in prompt_msgs]

    text_buf: list[str] = []
    err_raised: Exception | None = None

    try:
        async for event in client.stream_chat(
            messages=api_msgs,
            system_prompt="",
            tools=None,  # 摘要请求不传工具
        ):
            from llm.stream_events import CompletionDone, StreamError, TextChunk

            if isinstance(event, TextChunk):
                text_buf.append(event.text)
            elif isinstance(event, StreamError):
                err_raised = Exception(event.message)
                break
            elif isinstance(event, CompletionDone):
                # 自然结束，usage 不更新 anchor
                break
    except Exception as e:  # noqa: BLE001 —— 摘要请求需透传任何异常交由上层判断 PTL
        err_raised = e

    if err_raised is not None:
        raise err_raised

    return extract_summary("".join(text_buf))


# ── PTL 自重试 ────────────────────────────────────────────────────


async def ptl_retry(
    provider_config: Any,
    model: str,
    msgs: list[Message],
    first_err: Exception,
) -> str:
    """摘要请求撞 PTL 时的自重试。

    按 user turn 分组，前 PTL_RETRY_LIMIT 次每次丢最旧 1 组，
    之后按 ceil(剩余 × PTL_DROP_PERCENTAGE) 丢（至少 1 组），
    直到成功或全部丢光。

    Args:
        provider_config: ProviderConfig 实例。
        model: 模型名称。
        msgs: 被压缩的消息列表。
        first_err: 第一次失败的异常（用于非 PTL 异常判断）。

    Returns:
        解析后的摘要文本。

    Raises:
        Exception: 全部丢光仍失败或遇到非 PTL 异常。
    """
    from llm import PromptTooLongError

    groups = group_by_user_turn(msgs)
    total_attempts = 1  # 已尝试 1 次（first_err）
    last_err = first_err

    while groups:
        if total_attempts <= PTL_RETRY_LIMIT:
            # 前 PTL_RETRY_LIMIT 次：每次丢最旧 1 组
            groups = groups[1:]
        else:
            # 之后按比例丢
            drop = max(1, math.ceil(len(groups) * PTL_DROP_PERCENTAGE))
            groups = groups[drop:]

        if not groups:
            break

        flat_msgs = [m for g in groups for m in g]
        total_attempts += 1

        try:
            return await summarize_once(provider_config, model, flat_msgs)
        except PromptTooLongError as e:
            last_err = e
            continue
        except Exception:
            # 非 PTL 异常立即上抛
            raise

    # 全部丢光仍失败
    raise last_err


# ── run_summary ───────────────────────────────────────────────────


async def run_summary(
    provider_config: Any,
    model: str,
    recovery_snapshot: list[FileReadRecord],
    tool_defs: list[dict[str, Any]],
    old_msgs: list[Message],
) -> list[Message]:
    """执行一次完整的摘要 + 恢复 + 近期原文拼接。

    入口拍好恢复快照后传入，整个函数生命周期内只用这一份快照。

    Args:
        provider_config: ProviderConfig 实例。
        model: 模型名称。
        recovery_snapshot: RecoveryState.snapshot() 快照。
        tool_defs: 与 Request.tools 同源的工具定义列表。
        old_msgs: 当前对话全部消息。

    Returns:
        拼接好的新消息列表 [summary_user_msg, (optional assistant), ...recent]。

    Raises:
        Exception: 摘要生成失败（含 PTL 用光）。
    """
    from llm import PromptTooLongError

    # 1. 发摘要请求
    try:
        summary_text = await summarize_once(provider_config, model, old_msgs)
    except PromptTooLongError as e:
        summary_text = await ptl_retry(provider_config, model, old_msgs, e)

    # 2. 构造恢复段
    recovery_text = build_recovery_attachment(recovery_snapshot, tool_defs)

    # 3. 合并摘要 + 恢复到同一条 user 消息
    combined_content = "## 历史会话摘要\n" + summary_text + "\n\n" + recovery_text
    summary_and_recovery = Message(
        role=MessageRole.USER,
        content=combined_content,
        status=MessageStatus.COMPLETED,
    )

    # 4. 近期原文
    recent_tail = pick_recent_tail(old_msgs)

    # 5. 拼接（处理 role 衔接）
    return _join_after_summary(summary_and_recovery, recent_tail)


# ── auto_compact / force_compact ──────────────────────────────────


async def auto_compact(
    provider_config: Any,
    model: str,
    recovery_snapshot: list[FileReadRecord],
    tool_defs: list[dict[str, Any]],
    old_msgs: list[Message],
    auto_tracking: Any,
    estimated_token: int,
) -> tuple[list[Message], int, int]:
    """自动摘要压缩（含熔断器记录）。

    Args:
        provider_config: ProviderConfig 实例。
        model: 模型名称。
        recovery_snapshot: 恢复快照。
        tool_defs: 工具定义列表。
        old_msgs: 当前全部消息。
        auto_tracking: CompactCircuitBreaker 实例。
        estimated_token: 压缩前的估算 token。

    Returns:
        (new_msgs, before_tokens, after_tokens)。

    Raises:
        Exception: 摘要生成失败。
    """
    before_tok = estimated_token
    try:
        new_msgs = await run_summary(
            provider_config, model, recovery_snapshot, tool_defs, old_msgs
        )
    except Exception:
        auto_tracking.record_failure()
        raise

    auto_tracking.record_success()
    after_tok = estimate_tokens(0, new_msgs, 0)
    return new_msgs, before_tok, after_tok


async def force_compact(
    provider_config: Any,
    model: str,
    recovery_snapshot: list[FileReadRecord],
    tool_defs: list[dict[str, Any]],
    old_msgs: list[Message],
    estimated_token: int,
) -> tuple[list[Message], int, int]:
    """手动/紧急压缩（不调熔断器）。

    与 auto_compact 相同但不调 auto_tracking 任何方法。
    """
    before_tok = estimated_token
    new_msgs = await run_summary(
        provider_config, model, recovery_snapshot, tool_defs, old_msgs
    )
    after_tok = estimate_tokens(0, new_msgs, 0)
    return new_msgs, before_tok, after_tok
