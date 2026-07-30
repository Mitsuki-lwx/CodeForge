"""第一层预防性压缩。

单条工具结果与单消息聚合的落盘判断、磁盘写入、预览体构造。
纯函数风格，不修改入参。
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path

from conversation.message import Message, make_tool_result_block
from core.context_compression.const import (
    MESSAGE_AGGREGATE_LIMIT,
    PREVIEW_HEAD_BYTES,
    PREVIEW_HEAD_LINES,
    SINGLE_RESULT_LIMIT,
)
from core.context_compression.state import (
    ContentReplacementState,
    SessionContext,
)

logger = logging.getLogger(__name__)


# ── 落盘 I/O ────────────────────────────────────────────────────


def spill_single(
    session: SessionContext,
    tool_use_id: str,
    content: str,
) -> None:
    """把单条 tool_result 内容写入 spill_dir/<tool_use_id>。

    幂等：文件已存在则不重写、不报错。
    失败抛 OSError 由上层捕获降级。

    Args:
        session: 会话上下文（提供 spill_dir）。
        tool_use_id: 工具调用 ID（用作文件名）。
        content: 完整工具结果内容。
    """
    path = Path(session.spill_dir) / tool_use_id
    if path.exists():
        return
    path.write_bytes(content.encode("utf-8"))


# ── 预览体构造 ──────────────────────────────────────────────────


def _head_preview(content: str) -> str:
    """截取内容头部预览。

    先按行数截（PREVIEW_HEAD_LINES 行），再按字节二次裁剪
    （PREVIEW_HEAD_BYTES 字节），注意 UTF-8 边界对齐。
    """
    lines = content.splitlines(keepends=True)
    if len(lines) > PREVIEW_HEAD_LINES:
        lines = lines[:PREVIEW_HEAD_LINES]
    head = "".join(lines)
    # 按字节二次裁剪，保证不切断 UTF-8 多字节字符
    head_bytes = head.encode("utf-8")
    if len(head_bytes) > PREVIEW_HEAD_BYTES:
        # 从 PREVIEW_HEAD_BYTES 位置向前找合法的 UTF-8 边界
        truncated = head_bytes[:PREVIEW_HEAD_BYTES]
        head = truncated.decode("utf-8", errors="ignore")
    return head


def build_preview(
    original_bytes: int,
    head: str,
    spill_path: str,
) -> str:
    """构造替换体字符串。

    包含原始字节数、头部预览、落盘路径、重读提示。
    同一组入参两次调用返回逐字节相等字符串（纯函数，无副作用）。

    Args:
        original_bytes: 原始内容字节数。
        head: 头部预览文本。
        spill_path: 落盘文件完整路径。

    Returns:
        格式化的预览体字符串。
    """
    lines = [
        f"[content offloaded] original size: {original_bytes} bytes",
        f"[saved to] {spill_path}",
        "[head preview]",
        head,
        (
            "完整内容已保存到上述路径，如需查看请用文件读取工具读取该路径，"
            "不要凭头部预览猜测全文"
        ),
    ]
    return "\n".join(lines)


# ── offload_and_snip 主体 ────────────────────────────────────────


def offload_and_snip(
    msgs: list[Message],
    state: ContentReplacementState,
    session: SessionContext,
) -> list[Message]:
    """遍历消息列表，对 tool_result 做落盘 + 预览替换。

    规则：
      1. 已 Seen 的 tool_use_id → 通过 decide_once 复用存量决策
      2. 未决策的项按字节倒序处理：
         a. 单条 > SINGLE_RESULT_LIMIT → spill + replaced
         b. 聚合 > MESSAGE_AGGREGATE_LIMIT → 按倒序逐项落盘至 ≤ 阈值
         c. 未落盘的项 kept
      3. 落盘失败 → 降级为不替换、不写账本（"skip"）

    返回新的 list[Message]，不修改入参。

    Args:
        msgs: 对话消息列表。
        state: 替换决策账本。
        session: 会话上下文。

    Returns:
        处理后的消息列表（深拷贝）。
    """
    out = copy.deepcopy(msgs)

    for msg in out:
        # CodeForge 中 tool_result 挂在 role=user 消息的 content blocks 中
        if msg.role.value != "user":
            continue
        if not isinstance(msg.content, list):
            continue

        # 收集本消息中的 tool_result blocks
        tool_blocks: list[tuple[int, str, str]] = []  # (index, tool_use_id, content)
        for i, block in enumerate(msg.content):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                tid = block.get("tool_use_id", "")
                c = block.get("content", "")
                if isinstance(c, str):
                    tool_blocks.append((i, tid, c))

        if not tool_blocks:
            continue

        # 先处理已 Seen 的项
        candidates: list[tuple[int, str, str, int]] = []  # (idx, id, content, bytes)
        for idx, tid, content in tool_blocks:
            # 用 decide_once 探测是否已 Seen
            already_seen = tid in state._seen_ids
            if already_seen:
                new_content = state.decide_once(
                    tid,
                    content,
                    lambda: ("kept", ""),
                )
                msg.content[idx] = make_tool_result_block(tid, new_content)
            else:
                candidates.append((idx, tid, content, len(content.encode("utf-8"))))

        if not candidates:
            continue

        # 按字节倒序排序
        candidates.sort(key=lambda x: x[3], reverse=True)

        # 计算聚合字节（仅未 Seen 的项需判断聚合）
        remaining_bytes = sum(c[3] for c in candidates)

        for idx, tid, content, cb in candidates:

            def _decide(
                _tid=tid,
                _content=content,
                _idx=idx,
            ) -> tuple[str, str]:
                nonlocal remaining_bytes
                should_spill = False
                if (
                    len(_content.encode("utf-8")) > SINGLE_RESULT_LIMIT
                    or remaining_bytes > MESSAGE_AGGREGATE_LIMIT
                ):
                    should_spill = True

                if should_spill:
                    try:
                        spill_single(session, _tid, _content)
                    except OSError:
                        logger.warning(
                            "落盘失败 tool_use_id=%s，降级为保留原文",
                            _tid,
                        )
                        return ("skip", "")
                    spill_path = str(Path(session.spill_dir) / _tid)
                    preview = build_preview(
                        len(_content.encode("utf-8")),
                        _head_preview(_content),
                        spill_path,
                    )
                    remaining_bytes -= len(_content.encode("utf-8"))
                    return ("replaced", preview)
                else:
                    return ("kept", "")

            new_content = state.decide_once(tid, content, _decide)
            msg.content[idx] = make_tool_result_block(tid, new_content)

    return out
