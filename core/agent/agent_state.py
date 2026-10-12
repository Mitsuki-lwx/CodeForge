"""Agent 的会话状态 / 记忆 / hook / 模型切换。

从 `core/agent/agent.py` 抽出的一簇"周边"方法：会话状态存储、循环插件、
运行时换模型、记忆触发、hook 发射、回合收尾。它们都不参与单轮 ReAct 决策，
但每轮都会被调到。

用 Mixin 而不是独立对象：`self.` 保持不变，行为逐字不变，调用点零改动。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from conversation.manager import ConversationManager
from conversation.message import Message
from core.agent.runtime import SessionRuntime
from core.hooks.events import HookContext
from core.permissions.modes import PermissionMode
from core.prompts.builder import PromptBuilder
from core.tool.context import ExecutionContext
from core.trace.events import AgentErrorEvent
from llm.client import LLMClient

logger = logging.getLogger(__name__)


class StateMixin:
    """`Agent` 的会话状态、记忆、hook 与回合收尾。

    ── 对宿主的依赖契约 ──────────────────────────────────────────────
    属性由 `Agent.__init__` / 各 setter 赋值；`_emit_metric` / `_trace_record`
    由 `TelemetryMixin` 提供。纯注解对运行时零影响。
    """

    _conversation: ConversationManager
    _exec_ctx: ExecutionContext
    _client: LLMClient
    _prompt_builder: PromptBuilder
    _runtime: SessionRuntime | None
    _hooks: Any
    _state_store: Any
    _loop: Any
    _turn_finished: bool
    _memory_tasks: set[asyncio.Task]

    if TYPE_CHECKING:

        @property
        def permission_mode(self) -> PermissionMode: ...

        def _emit_metric(
            self, name: str, value: float, *, delta: bool = True
        ) -> None: ...
        def _trace_record(self, event: Any) -> None: ...

    def set_state_store(self, store) -> None:
        """注入会话状态存储（spec_session_state）。"""
        self._state_store = store

    def set_loop(self, loop) -> None:
        """运行时切换 Agent 循环策略（spec_loop）。"""
        self._loop = loop

    def switch_model(self, provider) -> None:
        """运行时切换主模型（/model spec_model_switch）。
        换 LLM 客户端、模型名、上下文窗口；**不 replace_history**（保留对话）。
        重复切到同一 provider 视为 no-op（LLMClient.create 幂等建客户端）。
        """
        from config.protocol_defaults import effective_context_window
        from llm.client import LLMClient

        self._client = LLMClient.create(provider)
        self._prompt_builder._model = provider.model
        if self._runtime is not None:
            self._runtime.context_window = effective_context_window(
                provider.protocol, provider.context_window
            )

    def _refresh_state(self) -> None:
        """刷新会话状态注入（约束→cached，目标+待办→uncached）。

        仅当文本变化时 set_state，避免无谓重建（保持缓存稳定）。
        """
        store = getattr(self, "_state_store", None)
        if store is None:
            return
        try:
            cons = store.list_constraints(include_persisted=True)
            constraints_text = (
                "## 硬性约束\n" + "\n".join(f"- {c['text']}" for c in cons)
                if cons
                else ""
            )
            goal = store.get_goal()
            todos = store.list_todos()
            parts: list[str] = []
            if goal:
                parts.append(f"## 当前目标\n{goal}")
            if todos:
                parts.append(
                    "## 待办\n"
                    + "\n".join(
                        f"- [{'x' if t['done'] else ' '}] {t['text']}" for t in todos
                    )
                )
            goals_todos_text = "\n".join(parts)
            if (
                constraints_text != self._prompt_builder._state_constraints
                or goals_todos_text != self._prompt_builder._state_goals_todos
            ):
                self._prompt_builder.set_state(constraints_text, goals_todos_text)
        except Exception:  # noqa: BLE001 —— 状态刷新失败静默，不阻断主流程
            return

    def _refresh_memory(self) -> None:
        """笔记更新后刷新记忆索引注入（仅当内容变化，保持缓存稳定）。"""
        # 会话状态注入随每轮刷新（约束/目标/待办可能被命令/工具/提炼改动）
        self._refresh_state()
        runtime = self._runtime
        if runtime is None or runtime.notes is None:
            return
        from core.notes import build_memory_index_text

        try:
            text = build_memory_index_text(runtime.notes)
        except Exception:  # noqa: BLE001 —— 索引读取失败降级为不刷新
            return
        if text != self._prompt_builder._memory:
            self._prompt_builder.set_injections(memory=text)

    def _recent_turn_messages(self) -> list[Message]:
        """取最后一条 user 文本消息（非 tool_result）起的消息，供记忆更新。"""
        msgs = self._conversation.messages
        from conversation.message import MessageRole

        start = -1
        for i in range(len(msgs) - 1, -1, -1):
            if msgs[i].role == MessageRole.USER and not msgs[i].tool_use_id:
                start = i
                break
        if start < 0:
            return msgs[-6:]
        return msgs[start:]

    def _maybe_trigger_memory(self, user_input: str) -> None:
        """每 5 轮或用户显式记忆请求时，后台异步更新笔记（F35/F36）。"""
        runtime = self._runtime
        if runtime is None or runtime.notes is None:
            return
        from core.notes import should_trigger_memory, update_memory

        runtime.turn_count += 1
        if not should_trigger_memory(runtime.turn_count, user_input):
            return
        provider = self._client.config
        store = runtime.notes
        recent = self._recent_turn_messages()
        # state 传入提炼通道：可写会话级目标/约束（spec_session_state）
        task = asyncio.create_task(
            update_memory(provider, store, recent, getattr(self, "_state_store", None))
        )
        self._memory_tasks.add(task)
        task.add_done_callback(self._memory_tasks.discard)

    async def shutdown_memory(self, timeout: float = 5.0) -> None:
        """退出前等待后台记忆任务（带超时，避免阻塞过久）。"""
        if not self._memory_tasks:
            return
        done, _ = await asyncio.wait(
            self._memory_tasks, timeout=timeout, return_when=asyncio.ALL_COMPLETED
        )
        for t in self._memory_tasks - done:
            t.cancel()

    # ── Hook helpers ─────────────────────────────────────────────────

    def _hook_ctx(self, event: str, **extra) -> HookContext:
        """构造事件 payload 上下文。extra 覆盖事件特化字段。"""
        runtime = self._runtime
        return HookContext(
            event=event,
            session_id=self._exec_ctx.session_id,
            cwd=str(self._exec_ctx.cwd),
            mode=self.permission_mode.value,
            turn_count=runtime.turn_count if runtime is not None else 0,
            **extra,
        )

    def _emit_hook(self, event: str, ctx: HookContext) -> None:
        """非拦截事件后台异步触发；hook 失败只记日志，绝不中断主流程。"""
        if self._hooks is None:
            return
        try:
            asyncio.create_task(self._hooks.run(event, ctx))
        except (RuntimeError, Exception):  # noqa: BLE001 —— 事件循环不可用时静默
            logger.warning("hook event %s dropped", event)

    def _finish_turn(self, user_input: str, error_code: str | None) -> None:
        """在所有 run() 出口触发 turn_end；出错时先发 agent_error。

        幂等:每轮只处理一次。hook 与 trace 各自独立,不因一方缺失而跳过另一方。
        """
        if self._turn_finished:
            return
        self._turn_finished = True
        if error_code:
            # T7: 出错终止 → 审计 agent_error(与 hook 的 agent_error 事件对应)
            self._trace_record(AgentErrorEvent(code=error_code))
            self._emit_metric("codeforge.agent.errors", 1, delta=True)
            if self._hooks is not None:
                self._emit_hook(
                    "agent_error", self._hook_ctx("agent_error", error_code=error_code)
                )
        if self._hooks is not None:
            self._emit_hook(
                "turn_end", self._hook_ctx("turn_end", user_input=user_input)
            )

    # ── Tool execution ───────────────────────────────────────────────
