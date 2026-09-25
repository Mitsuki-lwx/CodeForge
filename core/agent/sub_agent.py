"""子 Agent 运行时 —— run_to_completion 实现。

挂载到 Agent 上，复用主 run() 的 LLM 调用、工具执行、权限检查等基础设施，
但不产生 UI 事件（内部消费），返回最终文本。
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager
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


def _progress(agent: Any, action: str, detail: str = "") -> None:
    """往进度汇写一条「谁在做什么」；没挂汇时是 no-op。

    用途见 `core/tool/context.ProgressSink`：父 Agent 在 `await` 子 Agent 时自己的
    事件流是静默的，子 Agent 的进度只能由界面**主动来读**——这份记录就是给它读的。
    """
    sink = getattr(getattr(agent, "_exec_ctx", None), "progress", None)
    if sink is not None:
        sink.note(getattr(agent, "_agent_name", None) or "sub", action, detail)


def _clear_progress(agent: Any) -> None:
    """子 Agent 收尾时清空；否则父回到"等模型响应"时，界面还显示子 Agent 的旧状态。"""
    sink = getattr(getattr(agent, "_exec_ctx", None), "progress", None)
    if sink is not None:
        sink.clear()


# ── 审批应答机制：收口 + 安全网 ─────────────────────────────────────────
#
# `ask` 级决策必须有人应答，否则 `Agent._execute_tools` 会停在
# `await self._hitl_event.wait()` 上**无限期挂死**。应答者只有三种：
#   1. `unattended_policy`   权限层代答
#   2. `_dont_ask`           权限层转 allow（`--task` 无头）
#   3. `approval_upgrader`   审批通道冒泡，或有人调 `resolve_hitl`
#
# 三者原先由各入口**分别手工设置**，于是 `core/skills/executor.py` 的 fork Agent
# 三者全无、跑一条非白名单命令就挂死（实测复现见
# `.workbuddy-ai/verify_skill_fork_hang.py`）。这是"没有收口所以新入口必漏"的
# 第三次复发（前两次：`token_file` 静默忽略、`_run_http` 静默失败）。
# 裁定见 `docs/spec_subagent.md` 附录 B。


def _serving_upgrader(agent: Any) -> Any:
    """取「**有消费者在听**」的审批通道。

    「有实例」不等于「有人在听」：host / 无头 / 单测都可能挂了个没有消费者的
    通道，那种情况必须等同于"没有通道"——否则摘掉策略后请求无人应答，
    子 Agent 会变成一律被拒。
    """
    upgrader = getattr(getattr(agent, "_exec_ctx", None), "approval_upgrader", None)
    if upgrader is not None and not getattr(upgrader, "serving", False):
        return None
    return upgrader


def _has_ask_responder(agent: Any) -> bool:
    """该 Agent **当前已生效、且自足**的 `ask` 应答者。

    ⚠️ 「有策略」**不等于**「有人应答」：`review` 档**不自足** —— 它要靠挂着一个
    审查者（`_approval_reviewer`）才有意义。裸的 `review`（有档位、没审查者）会让
    `ask` 一路落到人工 HITL，无人值守下就是**无限期挂死**。
    所以这里对 `review` 额外要求审查者在位。

    这条判定是**防御性**的：将来若又出现新的"需要配套机制才自足"的档位，
    也会在这里被挡住，而不是靠每个入口自己记得。
    """
    if getattr(agent, "_approval_upgrader", None) is not None:
        return True
    if bool(getattr(agent, "_dont_ask", False)):
        return True
    policy = getattr(agent, "unattended_policy", None)
    if policy is None:
        return False
    from core.permissions.modes import UnattendedPolicy

    if policy is UnattendedPolicy.REVIEW:
        # `review` 必须同时有审查者才算应答者 —— 少了它就是挂死。
        return getattr(agent, "_approval_reviewer", None) is not None
    return True


def _shared_reviewer(parent: Any) -> Any:
    """父在 `review` 档且挂着审查者时，把**父的审查者**借给子 Agent。

    为什么共享是安全的：审查者是**每次调用无状态**的
    （`review(ctx) -> ReviewOutcome`，不持有会话、不累积状态），
    父子共用同一个实例不会互相污染。

    为什么要共享：`review` 档不自足，而子 Agent 拿不到审查者时只有两条路 ——
    挂死（裸 review）或退回最保守档（能力残废）。共享既保住语义
    （父用 Jev 审、子也用 Jev 审）又不降能力。
    """
    from core.permissions.modes import UnattendedPolicy

    if getattr(parent, "unattended_policy", None) is not UnattendedPolicy.REVIEW:
        return None
    return getattr(parent, "_approval_reviewer", None)


def _fallback_policy(parent: Any) -> Any:
    """兜底策略：按父的授权范围推导；拿不到父就取最保守档。

    ⚠️ **绝不返回 `review`**：它是唯一不自足的档位（要靠审查者）。
    这个函数的职责是"给一个装上去就能用的策略"，而裸 `review` 装上去会挂死。
    想保留 `review` 语义请走 `_shared_reviewer`（借审查者）。
    """
    from core.permissions.modes import UnattendedPolicy, resolve_child_policy

    if parent is None:
        return UnattendedPolicy.DENY_ALL
    try:
        policy = resolve_child_policy(parent)
    except Exception:  # noqa: BLE001 —— 推导失败也必须给出一个应答者
        return UnattendedPolicy.DENY_ALL
    if policy is UnattendedPolicy.REVIEW:
        return UnattendedPolicy.DENY_ALL
    return policy


@contextmanager
def arming_approval(agent: Any, parent: Any = None):
    """给「父在 `await` 等结果」的子 Agent 装审批应答机制（进入装、退出还原）。

    **新增入口只该调这一个函数**，不要各自拼那三处设置。

    决策顺序（详见 `docs/spec_subagent.md` 附录 B.3.1）：

    1. **有通道，且角色未显式声明 `dontask`** → 挂通道，并摘掉
       `unattended_policy`。策略若残留，`ask` 会在**权限层**就被代答，
       请求根本到不了通道——整个功能会是死的（附录 A 实测踩过）。
    2. **没有任何自足的应答者** → 先试着**借父的审查者**（父在 `review` 档时）；
       借不到再按 `parent` 的授权范围兜底。两种情况都记日志，绝不静默挂死。
    3. **已有应答者** → 什么都不做（既有入口零改动）。

    角色**显式** `dontask` 时不抢：能力清单第 9 条的层次是
    『父已批准账本 → 角色 `permission_mode` 兜底（含 `dontAsk`）→ 升级到主 TUI』，
    `dontAsk` 是中间层、先于升级，抢它会破坏角色契约。

    ⚠️ **第 2 条为什么先试"借审查者"**：`review` 档**不自足** —— 它要靠挂着
    审查者才有意义。旧实现在第 2 条直接按父的档位推导，于是父是 `review` 时
    子也拿到 `review`，但子**没有审查者** → `ask` 落到人工 HITL →
    **无人值守下无限期挂死**（实测复现见
    `.workbuddy-ai/verify_review_child_hang.py`）。
    """
    # 非真 Agent（单测里常见 `object()` 桩）没有可接入的机制 —— 原样透传，
    # 不写任何属性（否则直接 AttributeError）。
    if not hasattr(agent, "set_unattended_policy"):
        yield
        return

    from core.permissions.modes import UnattendedPolicy

    upgrader = _serving_upgrader(agent)
    prev_policy = getattr(agent, "unattended_policy", None)
    prev_upgrader = getattr(agent, "_approval_upgrader", None)
    prev_reviewer = getattr(agent, "_approval_reviewer", None)
    prev_dont_ask = bool(getattr(agent, "_dont_ask", False))
    changed = False
    try:
        if upgrader is not None and not prev_dont_ask:
            agent._approval_upgrader = upgrader
            # 摘策略让 `ask` 保持 `ask`。`_dont_ask` 不必动——上面的条件已保证
            # 角色没声明它（声明了就不会走这条分支）。
            agent.set_unattended_policy(None)
            changed = True
        elif not _has_ask_responder(agent):
            name = getattr(agent, "_agent_name", None) or "<unnamed>"
            shared = _shared_reviewer(parent)
            if shared is not None:
                # 借父的审查者，保住 `review` 语义（父用 Jev 审，子也用 Jev 审）。
                agent._approval_reviewer = shared
                agent.set_unattended_policy(UnattendedPolicy.REVIEW)
                changed = True
                logger.info(
                    "子 Agent（%s）借用父的审批审查者（%s），沿用 review 档 —— "
                    "review 不自足，不借就会挂在无人应答的审批上。",
                    name,
                    type(shared).__name__,
                )
            else:
                policy = _fallback_policy(parent)
                agent.set_unattended_policy(policy)
                changed = True
                logger.warning(
                    "子 Agent（%s）的 ask 级决策无人应答 → 已按 %s 兜底（%s），"
                    "避免无限期挂死。",
                    name,
                    getattr(policy, "value", policy),
                    "按父的授权范围推导"
                    if parent is not None
                    else "拿不到父，取最保守档",
                )
        yield
    finally:
        # 只有**改过**才还原：没改过就一个属性都不碰，保住"零改动"这条语义。
        if changed:
            agent._approval_upgrader = prev_upgrader
            agent._approval_reviewer = prev_reviewer
            agent.set_unattended_policy(prev_policy)
            agent._dont_ask = prev_dont_ask


def _ensure_ask_responder(agent: Any) -> None:
    """安全网：跑到底之前确认 `ask` 有人应答，没有就兜底 + 告警。

    收口（`arming_approval`）要求"新入口记得调它"，而人总会忘。这道守卫放在
    **无人值守跑到底的唯一入口** `run_to_completion` 开头，任何漏接线的入口
    最多只是拿到一条告警 + 请求被拒，不会再无限期挂死。

    刻意不还原：它在跑完即废的 Agent 上生效；若将来有"复用同一子 Agent 连跑
    多轮"的用法，需要重新审视这一条（见 spec 附录 B.5）。
    """
    if _has_ask_responder(agent):
        return
    from core.permissions.modes import UnattendedPolicy

    logger.warning(
        "子 Agent（%s）的 ask 级决策三种应答机制全无"
        "（无审批通道 / 无无人值守策略 / 未开 dont_ask）→ 按 deny_all 兜底。"
        "这通常意味着某个入口漏了 arming_approval() 接线。",
        getattr(agent, "_agent_name", None) or "<unnamed>",
    )
    if hasattr(agent, "set_unattended_policy"):
        agent.set_unattended_policy(UnattendedPolicy.DENY_ALL)


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
    # 安全网：`ask` 必须有人应答，否则会停在 `await` 上无限期挂死。
    # 放在最开头、`agent._loop` 分支**之前**，这样连自定义 loop 也覆盖。
    _ensure_ask_responder(agent)

    loop = getattr(agent, "_loop", None)
    if loop is not None:
        # 经 loop 策略（spec_loop）：默认 ReactLoop 走底层，自定义 loop 走用户逻辑
        return await loop.run_to_completion(agent, conv, task, events)
    try:
        return await _run_loop(agent, conv, task, events)
    finally:
        _clear_progress(agent)
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

        _progress(agent, "等模型响应")
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
        _progress(agent, "调工具", ", ".join(t.name for t in tool_uses))
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
