"""子 Agent 运行时 —— run_to_completion 实现。

挂载到 Agent 上，复用主 run() 的 LLM 调用、工具执行、权限检查等基础设施，
但不产生 UI 事件（内部消费），返回最终文本。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from conversation.manager import ConversationManager
from core.agent.events import AgentError
from core.agent.plan_mode import (
    build_plan_mode_reminder,
)
from core.context_compression.compact import (
    ManageInput,
    TriggerKind,
    manage_context,
)
from core.context_compression.const import (
    AUTO_SAFETY_MARGIN,
    SUMMARY_RESERVE,
)
from core.context_compression.token import estimate_tokens
from llm import PromptTooLongError
from llm.stream_events import (
    CompletionDone,
    StreamError,
    TextChunk,
    ThinkingChunk,
    ToolUse,
)

logger = logging.getLogger(__name__)


async def run_to_completion(
    agent: Any,  # Agent 实例（避免循环导入）
    conv: ConversationManager,
    task: str = "",
    events: asyncio.Queue | None = None,
) -> str:
    """执行子 Agent 的“跑到底”循环。

    复用主 Agent 的 LLM 流式调用、工具执行、权限检查等基础设施。
    与主 ``run()`` 的区别：
    - 不产生 UI 事件（内部消费）
    - 最终返回最后一条 assistant 文本
    - 不触发 memory update / compact reminder 等主对话专属逻辑
    - 接受可选的 events 队列，把内部事件转发出去供 TaskManager 聚合

    Args:
        agent: Agent 实例（type: Any 避免循环导入）。
        conv: 子 Agent 的 ConversationManager（已装填或空白）。
        task: 子任务描述。非空时追加为 user 消息。
        events: 可选的外部事件队列，Tool/Text 事件会被 put 进去。

    Returns:
        最后一条 assistant 消息的文本内容。

    Raises:
        MaxTurnsReached: 触达 max_turns 时抛出，携带最后文本。
        asyncio.CancelledError: 被取消时透传。
    """
    loop = getattr(agent, "_loop", None)
    if loop is not None:
        # 经 loop 策略（spec_loop）：默认 ReactLoop 走底层，自定义 loop 走用户逻辑
        return await loop.run_to_completion(agent, conv, task, events)
    try:
        return await _run_loop(agent, conv, task, events)
    finally:
        # ── 隔离 worktree 清理：子 Agent 结束（含取消/异常）时触发 ──
        _cleanup_worktree(agent)


async def _run_loop(
    agent: Any,
    conv: ConversationManager,
    task: str,
    events: asyncio.Queue | None,
) -> str:
    """run_to_completion 的 ReAct 循环主体。"""
    # ── 装填任务 ──
    if task:
        conv.add_user_message(task)

    start_time = time.monotonic()
    max_turns = agent.max_turns
    emergency_retried = False
    iteration = 0
    last_text = ""

    while iteration < max_turns:
        iteration += 1

        # 取消检查
        if agent._cancel.is_set():
            # 重置 cancel 标记以便下次使用
            agent._cancel.clear()
            raise asyncio.CancelledError("Sub-agent cancelled")

        # ── Plan Mode 提醒（如果子 Agent 在 plan 模式）──
        if agent.plan_mode and agent._plan_path:
            plan_path_str = str(agent._plan_path)
            plan_exists = agent._plan_path.exists()
            reminder = build_plan_mode_reminder(plan_path_str, plan_exists, iteration)
            conv.add_system_reminder(reminder)

        # ── 上下文压缩：每轮自动检查 ──
        if agent._runtime is not None:
            try:
                await _auto_compact(agent, conv)
            except Exception:
                logger.warning(
                    "sub-agent auto-compact failed, continuing", exc_info=True
                )

        # ── 一轮 LLM 流式调用 ──
        api_messages, _ = conv.to_api_format()
        # 迭代级诊断：把「迭代号 + 喂入 payload 字符数」写进本轮 LLM span（纯计算，no-op 安全）
        try:
            from core.context_compression.token import message_chars
            from core.observability.context import (
                set_agent_identity,
                set_iteration_meta,
            )

            set_iteration_meta(iteration, message_chars(api_messages))
            # 子 Agent 身份：id=session_id（{parent}-sub 唯一），name=Agent 工具注入的可读名
            set_agent_identity(
                str(agent._exec_ctx.session_id),
                getattr(agent, "_agent_name", None) or "sub",
            )
        except Exception:  # noqa: BLE001 —— 观测辅助失败静默
            return
        tools = agent._build_tool_defs()

        env_info = __import__(
            "core.prompts.environment", fromlist=["collect_environment"]
        ).collect_environment(
            work_dir=str(agent._exec_ctx.cwd),
            model=agent._prompt_builder._model,
            version=agent._prompt_builder._version,
        )

        if agent._system_prompt_override is not None:
            full_system = agent._system_prompt_override
            if env_info.strip():
                full_system = full_system + "\n\n" + env_info.strip()
            sys_kwargs = {"system_prompt": full_system}
        else:
            assembly = agent._prompt_builder.build_assembly(env_info)
            # 稳定/变化块分离走 system_blocks，adapter 打 cache_control 断点命中缓存
            sys_kwargs = {"system_blocks": assembly}

        stream_msg = conv.start_assistant_stream()
        tool_uses: list[ToolUse] = []
        unknown_count = 0
        error_event = None

        try:
            async for se in agent._client.stream_chat(
                api_messages,
                tools=tools or None,
                **sys_kwargs,
            ):
                if agent._cancel.is_set():
                    conv.finish_stream(stream_msg)
                    agent._cancel.clear()
                    raise asyncio.CancelledError("Sub-agent cancelled")

                if isinstance(se, ThinkingChunk):
                    if events is not None:
                        try:
                            events.put_nowait(("thinking", se.text))
                        except asyncio.QueueFull:
                            pass

                elif isinstance(se, TextChunk):
                    conv.append_to_stream(stream_msg, se.text)
                    last_text += se.text
                    if events is not None:
                        try:
                            events.put_nowait(("text", se.text))
                        except asyncio.QueueFull:
                            pass

                elif isinstance(se, ToolUse):
                    tool_uses.append(se)
                    if not _registry_has(agent, se.name):
                        unknown_count += 1

                elif isinstance(se, CompletionDone):
                    conv.finish_stream(stream_msg, se.usage)
                    if se.usage:
                        agent._total_usage["input_tokens"] += se.usage.get(
                            "input_tokens", 0
                        )
                        agent._total_usage["output_tokens"] += se.usage.get(
                            "output_tokens", 0
                        )

                elif isinstance(se, StreamError):
                    conv.fail_stream(stream_msg, se.message)
                    error_event = AgentError(message=se.message, code="stream_error")

        except PromptTooLongError as e:
            conv.fail_stream(stream_msg, str(e))
            if agent._runtime is not None and not emergency_retried:
                emergency_retried, _ = await _emergency_compact(agent, conv, e)
                if emergency_retried:
                    continue
            error_event = AgentError(message=str(e), code="ptl_error")

        except Exception as e:  # noqa: BLE001 —— 流式错误转 error_event
            conv.fail_stream(stream_msg, str(e))
            error_event = AgentError(message=str(e), code="stream_error")

        if error_event:
            # 返回含错误信息的文本
            if last_text:
                return last_text
            return f"Error: {error_event.message}"

        if unknown_count >= agent._config.unknown_tool_threshold:
            if last_text:
                return last_text
            return f"Error: Too many unknown tool requests ({unknown_count})"

        # ── 本轮无工具调用 → 完成 ──
        if not tool_uses:
            return last_text or _get_last_assistant_text(conv)

        # ── 执行工具 ──
        if events is not None:
            try:
                events.put_nowait(("tools_start", len(tool_uses)))
            except asyncio.QueueFull:
                pass

        async for ev in agent._execute_tools(tool_uses):
            # 工具事件内部消费，不 yield
            if events is not None:
                try:
                    if hasattr(ev, "name"):
                        events.put_nowait(
                            ("tool", ev.name if hasattr(ev, "name") else str(ev))
                        )
                except asyncio.QueueFull:
                    pass

        # ── 检测 ExitPlanMode ──
        exit_plan_called = any(tu.name == "ExitPlanMode" for tu in tool_uses)
        if exit_plan_called and agent.plan_mode:
            return last_text or _get_last_assistant_text(conv)

    # ── 触达 max_turns ──
    elapsed = time.monotonic() - start_time
    final = last_text or _get_last_assistant_text(conv)
    logger.info("sub-agent reached max_turns (%d) after %.1fs", max_turns, elapsed)
    return final


# ── 辅助函数 ──────────────────────────────────────────────────────


def _cleanup_worktree(agent: Any) -> None:
    """子 Agent 结束时清理隔离 worktree（若有）。

    WorktreeManager 挂在 agent._wt_manager，session 在 agent._worktree_session。
    有未提交变更/未推送 commit → 保留 + 记日志（主 Agent 后续可见）。
    清理为后台尽力而为：失败仅告警，绝不中断。
    """
    wt_session = getattr(agent, "_worktree_session", None)
    wt_manager = getattr(agent, "_wt_manager", None)
    if wt_session is None or wt_manager is None:
        return

    import asyncio

    async def _do() -> None:
        try:
            result = await wt_manager.cleanup(wt_session)
            if result.kept:
                logger.warning("worktree %s kept: %s", wt_session.name, result.reason)
            else:
                logger.info("worktree %s cleaned up", wt_session.name)
        except Exception as e:  # noqa: BLE001 —— 清理失败仅告警
            logger.warning("worktree cleanup failed: %s", e)

    try:
        asyncio.create_task(_do())
    except RuntimeError:
        logger.warning("worktree cleanup skipped (no running loop)")


def _registry_has(agent: Any, name: str) -> bool:
    try:
        agent._registry.get(name)
        return True
    except Exception:  # noqa: BLE001 —— 工具不存在时视为未知
        return False


def _get_last_assistant_text(conv: ConversationManager) -> str:
    for m in reversed(conv.messages):
        from conversation.message import MessageRole

        if m.role == MessageRole.ASSISTANT and m.content:
            return m.content
    return ""


async def _auto_compact(agent: Any, conv: ConversationManager) -> None:
    """子 Agent 的上下文压缩检查（简化版：不产生事件）。"""
    runtime = agent._runtime
    if runtime is None or runtime.session is None:
        return

    before_len = len(conv.messages)
    est = estimate_tokens(runtime.usage_anchor, conv.messages, runtime.anchor_msg_len)

    min_window = SUMMARY_RESERVE + AUTO_SAFETY_MARGIN
    threshold = runtime.context_window - SUMMARY_RESERVE - AUTO_SAFETY_MARGIN
    if runtime.context_window <= min_window or est < threshold:
        return
    if runtime.auto_tracking.tripped():
        return

    in_ = ManageInput(
        conv=conv,
        provider_config=agent._client.config,
        model=agent._client.config.model,
        context_window=runtime.context_window,
        tool_defs=agent._build_tool_defs(),
        replacement=runtime.replacement,
        recovery=runtime.recovery,
        auto_tracking=runtime.auto_tracking,
        session=runtime.session,
        usage_anchor=runtime.usage_anchor,
        anchor_msg_len=runtime.anchor_msg_len,
        estimated_token=est,
        trigger=TriggerKind.AUTO,
    )

    try:
        await manage_context(in_)
        if len(conv.messages) < before_len:
            runtime.usage_anchor = 0
            runtime.anchor_msg_len = 0
    except Exception as e:  # noqa: BLE001 —— 自动压缩失败降级，不中断子 Agent
        logger.warning("sub-agent auto-compact failed: %s", e)


async def _emergency_compact(
    agent: Any,
    conv: ConversationManager,
    err: Exception,
) -> tuple[bool, Any]:
    """子 Agent 的紧急压缩（简化版：不产生事件）。"""
    runtime = agent._runtime
    if runtime is None or runtime.session is None:
        return False, None

    before_est = estimate_tokens(
        runtime.usage_anchor, conv.messages, runtime.anchor_msg_len
    )
    in_ = ManageInput(
        conv=conv,
        provider_config=agent._client.config,
        model=agent._client.config.model,
        context_window=runtime.context_window,
        tool_defs=agent._build_tool_defs(),
        replacement=runtime.replacement,
        recovery=runtime.recovery,
        auto_tracking=runtime.auto_tracking,
        session=runtime.session,
        usage_anchor=runtime.usage_anchor,
        anchor_msg_len=runtime.anchor_msg_len,
        estimated_token=before_est,
        trigger=TriggerKind.EMERGENCY,
    )

    try:
        await manage_context(in_)
    except Exception as e:  # noqa: BLE001 —— 紧急压缩失败降级
        logger.warning("sub-agent emergency-compact failed: %s", e)
        return False, None

    runtime.usage_anchor = 0
    runtime.anchor_msg_len = 0
    retry_est = estimate_tokens(0, conv.messages, 0)
    from core.context_compression.const import MANUAL_SAFETY_MARGIN

    retryable = retry_est < runtime.context_window - MANUAL_SAFETY_MARGIN
    return retryable, None


# ── 挂载到 Agent 类 ──


def attach_to_agent():
    """将 run_to_completion 方法挂载到 Agent 类上。

    在 core.agent.__init__.py 中调用一次即可。
    """
    from core.agent.agent import Agent

    async def _run_to_completion(
        self: Agent,
        conv: ConversationManager,
        task: str = "",
        events: asyncio.Queue | None = None,
    ) -> str:
        return await run_to_completion(self, conv, task, events)

    Agent.run_to_completion = _run_to_completion  # type: ignore[method-assign]
