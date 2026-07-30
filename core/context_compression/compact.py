"""manage_context 主入口。

Agent 每轮请求前必调的唯一入口。
编排两层调用：第一层 offload_and_snip（预防）+ 第二层 auto_compact（兜底）。
支持 AUTO / MANUAL / EMERGENCY 三种触发模式。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from conversation.manager import ConversationManager
from conversation.message import Message
from core.context_compression.const import (
    AUTO_SAFETY_MARGIN,
    SUMMARY_RESERVE,
)
from core.context_compression.layer1 import offload_and_snip
from core.context_compression.layer2 import auto_compact, force_compact
from core.context_compression.state import (
    CompactCircuitBreaker,
    ContentReplacementState,
    RecoveryState,
    SessionContext,
)
from core.context_compression.token import estimate_tokens

logger = logging.getLogger(__name__)


def _replace_if_changed(conv: ConversationManager, new_msgs: list[Message]) -> None:
    """仅当内容实际变化时才替换历史。

    offload_and_snip 对未落盘的消息是内容等价的深拷贝；
    无条件 replace_history 会让 on_replace 存档回调每轮
    写一次 compact 标记并重写全部历史（JSONL 膨胀重复）。
    """
    if new_msgs != conv.messages:
        conv.replace_history(new_msgs)


# ── 枚举与数据类 ──────────────────────────────────────────────────


class TriggerKind(Enum):
    """压缩触发模式。"""

    AUTO = "auto"  # 自动触发（每轮检查阈值 + 熔断）
    MANUAL = "manual"  # 手动触发（/compress 命令，无视阈值和熔断）
    EMERGENCY = "emergency"  # 紧急触发（PTL 恢复，先 layer1 再 force_compact）


@dataclass
class ManageInput:
    """manage_context 的输入参数。"""

    conv: ConversationManager
    provider_config: Any  # ProviderConfig 实例
    model: str
    context_window: int
    tool_defs: list[dict[str, Any]]
    replacement: ContentReplacementState
    recovery: RecoveryState
    auto_tracking: CompactCircuitBreaker
    session: SessionContext
    usage_anchor: int
    anchor_msg_len: int
    estimated_token: int
    trigger: TriggerKind


@dataclass
class ManageOutput:
    """manage_context 的返回结果。"""

    before_tokens: int
    after_tokens: int


# ── 主入口 ───────────────────────────────────────────────────────


async def manage_context(in_: ManageInput) -> ManageOutput:
    """Agent 每轮请求前必调的唯一入口。

    步骤：
      MANUAL → 跳过 layer1 + 阈值 + 熔断，直接 force_compact
      EMERGENCY → 先强制 offload_and_snip，再 force_compact
      AUTO → offload_and_snip → 重估 token → 阈值判断 → auto_compact

    before_tokens = in_.estimated_token
    after_tokens = 压缩后重估算的值（仅 layer1 时 = layer1 后的估算）

    Args:
        in_: 压缩输入参数。

    Returns:
        ManageOutput(before_tokens, after_tokens)。
    """
    # ── SANITY CHECK: context_window 过小 ──
    min_window = SUMMARY_RESERVE + AUTO_SAFETY_MARGIN
    if in_.context_window <= min_window and in_.trigger == TriggerKind.AUTO:
        logger.warning(
            f"context_window={in_.context_window} ≤ {min_window}，"
            "跳过自动 layer2（避免死循环）"
        )

    # ═══════════════════════════════════════════════════════════
    # MANUAL 分支
    # ═══════════════════════════════════════════════════════════
    if in_.trigger == TriggerKind.MANUAL:
        snapshot = in_.recovery.snapshot()
        new_msgs, before, after = await force_compact(
            provider_config=in_.provider_config,
            model=in_.model,
            recovery_snapshot=snapshot,
            tool_defs=in_.tool_defs,
            old_msgs=in_.conv.messages,
            estimated_token=in_.estimated_token,
        )
        in_.conv.replace_history(new_msgs)
        return ManageOutput(before_tokens=before, after_tokens=after)

    # ═══════════════════════════════════════════════════════════
    # EMERGENCY 分支
    # ═══════════════════════════════════════════════════════════
    if in_.trigger == TriggerKind.EMERGENCY:
        # 先强制 layer1 挪走大工具结果
        layer1_out = offload_and_snip(in_.conv.messages, in_.replacement, in_.session)
        _replace_if_changed(in_.conv, layer1_out)

        # 再 force_compact
        snapshot = in_.recovery.snapshot()
        new_msgs, before, after = await force_compact(
            provider_config=in_.provider_config,
            model=in_.model,
            recovery_snapshot=snapshot,
            tool_defs=in_.tool_defs,
            old_msgs=in_.conv.messages,
            estimated_token=in_.estimated_token,
        )
        in_.conv.replace_history(new_msgs)
        return ManageOutput(before_tokens=before, after_tokens=after)

    # ═══════════════════════════════════════════════════════════
    # AUTO 分支
    # ═══════════════════════════════════════════════════════════
    # a. Layer 1: offload_and_snip
    layer1_out = offload_and_snip(in_.conv.messages, in_.replacement, in_.session)
    _replace_if_changed(in_.conv, layer1_out)

    # b. 重估 token（用 layer1_out，不是 in_.estimated_token）
    est_tokens = estimate_tokens(in_.usage_anchor, layer1_out, in_.anchor_msg_len)

    # c. 阈值判断
    if in_.context_window <= min_window:
        # sanity check 失败：跳过 layer2
        return ManageOutput(
            before_tokens=in_.estimated_token,
            after_tokens=est_tokens,
        )

    threshold = in_.context_window - SUMMARY_RESERVE - AUTO_SAFETY_MARGIN
    if est_tokens < threshold:
        # 未达阈值，仅 layer1 生效
        return ManageOutput(
            before_tokens=in_.estimated_token,
            after_tokens=est_tokens,
        )

    if in_.auto_tracking.tripped():
        # 熔断器跳闸，跳过自动 layer2
        logger.info("熔断器已跳闸，跳过自动 layer2")
        return ManageOutput(
            before_tokens=in_.estimated_token,
            after_tokens=est_tokens,
        )

    # d. auto_compact
    snapshot = in_.recovery.snapshot()
    try:
        new_msgs, before, after = await auto_compact(
            provider_config=in_.provider_config,
            model=in_.model,
            recovery_snapshot=snapshot,
            tool_defs=in_.tool_defs,
            old_msgs=in_.conv.messages,
            auto_tracking=in_.auto_tracking,
            estimated_token=est_tokens,
        )
    except Exception:
        logger.exception("auto_compact 失败")
        return ManageOutput(
            before_tokens=in_.estimated_token,
            after_tokens=est_tokens,
        )

    in_.conv.replace_history(new_msgs)
    return ManageOutput(before_tokens=before, after_tokens=after)
