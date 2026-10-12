"""Agent 的上下文压缩编排 —— Layer1 预防 + Layer2 兜底 + 紧急压缩。

从 `core/agent/agent.py` 抽出的一簇方法（自动压缩 / 紧急压缩 / 手动 `/compress`）。
它们只读写 `_runtime` / `_conversation` / `_client` / `_hooks`，与 ReAct 循环的
主流程通过事件流解耦（yield `CompactEvent`），适合单独一个文件。

用 Mixin 而不是独立对象：`self.` 保持不变，行为逐字不变，调用点零改动。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Any

from conversation.manager import ConversationManager
from core.agent.events import AgentEvent, CompactEvent, CompactPhase
from core.agent.runtime import SessionRuntime
from core.context_compression.compact import (
    ManageInput,
    TriggerKind,
    manage_context,
)
from core.context_compression.const import (
    AUTO_SAFETY_MARGIN,
    MANUAL_SAFETY_MARGIN,
    SUMMARY_RESERVE,
)
from core.context_compression.token import estimate_tokens
from core.hooks.events import HookContext
from core.trace.events import CompactEvent as TraceCompactEvent
from llm.client import LLMClient

logger = logging.getLogger(__name__)


class CompactionMixin:
    """`Agent` 的上下文压缩编排。

    ── 对宿主的依赖契约 ──────────────────────────────────────────────
    属性由 `Agent.__init__` 赋值；方法由**兄弟 Mixin** 提供（`StateMixin` 的
    `_emit_hook` / `_hook_ctx`、`TelemetryMixin` 的 `_trace_record`、
    `Agent` 自己的 `_build_tool_defs`）。纯注解/类型桩对运行时零影响。
    """

    _client: LLMClient
    _conversation: ConversationManager
    _runtime: SessionRuntime | None
    _hooks: Any

    if TYPE_CHECKING:

        def _build_tool_defs(self) -> list[dict[str, Any]]: ...
        def _emit_hook(self, event: str, ctx: HookContext) -> None: ...
        def _hook_ctx(self, event: str, **extra: Any) -> HookContext: ...
        def _trace_record(self, event: Any) -> None: ...

    def _build_manage_input(
        self, estimated_token: int, trigger: TriggerKind
    ) -> ManageInput:
        runtime = self._runtime
        assert runtime is not None and runtime.session is not None
        return ManageInput(
            conv=self._conversation,
            provider_config=self._client.config,
            model=self._client.config.model,
            context_window=runtime.context_window,
            tool_defs=self._build_tool_defs(),
            replacement=runtime.replacement,
            recovery=runtime.recovery,
            auto_tracking=runtime.auto_tracking,
            session=runtime.session,
            usage_anchor=runtime.usage_anchor,
            anchor_msg_len=runtime.anchor_msg_len,
            estimated_token=estimated_token,
            trigger=trigger,
        )

    async def _auto_compact_if_needed(self) -> AsyncGenerator[AgentEvent, None]:
        """每轮请求前自动两层检查（layer1 + 阈值判断 + layer2）。

        layer1 静默执行不发事件；仅在 layer2 可能被触发时
        emit BEFORE_AUTO / AFTER_AUTO 一对事件。
        """
        runtime = self._runtime
        if runtime is None or runtime.session is None:
            return
        conv = self._conversation
        before_len = len(conv.messages)
        est = estimate_tokens(
            runtime.usage_anchor, conv.messages, runtime.anchor_msg_len
        )

        # 预判 layer2 是否会被触发（与 manage_context 内部阈值一致），
        # 决定是否 emit 状态事件
        min_window = SUMMARY_RESERVE + AUTO_SAFETY_MARGIN
        threshold = runtime.context_window - SUMMARY_RESERVE - AUTO_SAFETY_MARGIN
        may_compact = (
            runtime.context_window > min_window
            and est >= threshold
            and not runtime.auto_tracking.tripped()
        )

        in_ = self._build_manage_input(est, TriggerKind.AUTO)
        if may_compact:
            if self._hooks is not None:
                self._emit_hook(
                    "context_compact",
                    self._hook_ctx(
                        "context_compact", trigger="auto", before_tokens=est
                    ),
                )
            yield CompactEvent(phase=CompactPhase.BEFORE_AUTO, before=est)
        try:
            output = await manage_context(in_)
        except Exception as e:  # noqa: BLE001 —— 自动压缩失败不影响主流程，仅告警
            logger.warning("auto_compact 意外失败: %s", e)
            if may_compact:
                yield CompactEvent(
                    phase=CompactPhase.AFTER_AUTO, before=est, after=est, err=e
                )
            return
        # layer2 运行后历史被替换（条数骤减）→ 锚点作废，待下一轮主 stream 重建
        layer2_ran = len(conv.messages) < before_len
        if layer2_ran:
            runtime.usage_anchor = 0
            runtime.anchor_msg_len = 0
        if may_compact:
            if self._hooks is not None:
                self._emit_hook(
                    "context_compact",
                    self._hook_ctx(
                        "context_compact",
                        trigger="auto",
                        before_tokens=output.before_tokens,
                        after_tokens=output.after_tokens,
                    ),
                )
            # T7: 压缩 → 审计 compact(before/after token)
            self._trace_record(
                TraceCompactEvent(
                    phase="after_auto",
                    before_tokens=output.before_tokens,
                    after_tokens=output.after_tokens,
                )
            )
            # 压缩「达标」判定：after_tokens 降到阈值以下即可——可能是 layer1 单独
            # 落盘就压够了（此时 layer2 不会跑，消息条数不变），不该误报「layer2 未完成」。
            threshold = runtime.context_window - SUMMARY_RESERVE - AUTO_SAFETY_MARGIN
            reached = output.after_tokens < threshold
            yield CompactEvent(
                phase=CompactPhase.AFTER_AUTO,
                before=output.before_tokens,
                after=output.after_tokens,
                err=None if reached else RuntimeError("压缩未达标"),
            )

    async def _emergency_compact(
        self, err: Exception
    ) -> tuple[bool, CompactEvent | None]:
        """PTL 紧急压缩：先 layer1 挪走大工具结果，再 force_compact 重建历史。

        Returns:
            (是否可重试主对话 stream, AFTER_EMERGENCY 事件)。
        """
        runtime = self._runtime
        assert runtime is not None and runtime.session is not None
        before_est = estimate_tokens(
            runtime.usage_anchor,
            self._conversation.messages,
            runtime.anchor_msg_len,
        )
        in_ = self._build_manage_input(before_est, TriggerKind.EMERGENCY)
        try:
            output = await manage_context(in_)
        except Exception as e:  # noqa: BLE001 —— 紧急压缩失败需上报事件
            logger.warning("紧急压缩失败: %s", e)
            return False, CompactEvent(
                phase=CompactPhase.AFTER_EMERGENCY,
                before=before_est,
                after=before_est,
                err=e,
            )
        # 历史已重建，锚点作废
        runtime.usage_anchor = 0
        runtime.anchor_msg_len = 0
        if self._hooks is not None:
            self._emit_hook(
                "context_compact",
                self._hook_ctx(
                    "context_compact",
                    trigger="emergency",
                    before_tokens=output.before_tokens,
                    after_tokens=output.after_tokens,
                ),
            )
        retry_est = estimate_tokens(0, self._conversation.messages, 0)
        retryable = retry_est < runtime.context_window - MANUAL_SAFETY_MARGIN
        # T7: 紧急压缩 → 审计 compact(phase=after_emergency)
        self._trace_record(
            TraceCompactEvent(
                phase="after_emergency",
                before_tokens=output.before_tokens,
                after_tokens=output.after_tokens,
            )
        )
        return retryable, CompactEvent(
            phase=CompactPhase.AFTER_EMERGENCY,
            before=output.before_tokens,
            after=output.after_tokens,
        )

    async def run_force_compact(self) -> tuple[int, int]:
        """手动 /compress 压缩，无视阈值与熔断。

        Returns:
            (before_tokens, after_tokens)。
        """
        runtime = self._runtime
        if runtime is None or runtime.session is None:
            raise RuntimeError("会话压缩运行时未初始化")
        est = estimate_tokens(
            runtime.usage_anchor,
            self._conversation.messages,
            runtime.anchor_msg_len,
        )
        in_ = self._build_manage_input(est, TriggerKind.MANUAL)
        output = await manage_context(in_)
        runtime.usage_anchor = 0
        runtime.anchor_msg_len = 0
        if self._hooks is not None:
            self._emit_hook(
                "context_compact",
                self._hook_ctx(
                    "context_compact",
                    trigger="manual",
                    before_tokens=output.before_tokens,
                    after_tokens=output.after_tokens,
                ),
            )
        # T7: 手动压缩 → 审计 compact
        self._trace_record(
            TraceCompactEvent(
                phase="after_auto",
                before_tokens=output.before_tokens,
                after_tokens=output.after_tokens,
            )
        )
        return output.before_tokens, output.after_tokens
