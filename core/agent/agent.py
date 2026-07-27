"""Agent 核心 —— ReAct 循环编排。

负责：带工具定义调 LLM → 流式收集 → 执行工具（权限检查） → 结果回灌 → 循环，
直到模型给出不含工具调用的纯文本回复（或触发停止条件）。

集成上下文压缩：每轮自动执行两层检查（Layer1 预防 + Layer2 兜底），
支持手动 /compress 和 PromptTooLongError 紧急压缩。
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, AsyncGenerator

from conversation.manager import ConversationManager
from conversation.message import Message
from core.agent.config import AgentConfig
from core.agent.events import (
    AgentError,
    AgentEvent,
    AgentFinished,
    CompactEvent,
    CompactPhase,
    HITLRequired,
    IterationUpdate,
    PlanReady,
    TextDelta,
    ThinkingDelta,
    ToolCallFinished,
    ToolCallStarted,
)
from core.agent.plan_mode import (
    PlanMode,
    build_plan_mode_reminder,
    detect_plan_intent,
    generate_plan_path,
)
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
from core.context_compression.token import estimate_tokens, usage_anchor
from core.permissions.checker import Decision, PermissionChecker
from core.permissions.dangerous import DangerousCommandDetector
from core.permissions.modes import PermissionMode
from core.permissions.rules import RuleEngine, extract_content
from core.permissions.sandbox import PathSandbox
from core.prompts.builder import PromptBuilder
from core.prompts.environment import collect_environment
from core.tool.context import ExecutionContext
from core.tool.registry import ToolRegistry
from llm import PromptTooLongError
from llm.client import LLMClient
from llm.stream_events import (
    CompletionDone,
    StreamError,
    TextChunk,
    ThinkingChunk,
    ToolUse,
)

logger = logging.getLogger(__name__)


class Agent:
    """ReAct 循环编排器。"""

    def __init__(
        self,
        registry: ToolRegistry,
        llm_client: LLMClient,
        exec_ctx: ExecutionContext,
        conversation: ConversationManager,
        config: AgentConfig | None = None,
        runtime: SessionRuntime | None = None,
        instructions: str = "",
        memory: str = "",
    ) -> None:
        self._registry = registry
        self._client = llm_client
        self._exec_ctx = exec_ctx
        self._conversation = conversation
        self._config = config or AgentConfig()
        self._mode: PlanMode = PlanMode.OFF
        self._cancel: asyncio.Event = asyncio.Event()
        self._total_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._runtime = runtime
        # 后台记忆更新任务集合（退出时等待）
        self._memory_tasks: set[asyncio.Task] = set()

        # ── Permission system ──
        self._sandbox = PathSandbox(work_dir=str(exec_ctx.cwd))
        self._detector = DangerousCommandDetector()
        self._rule_engine = RuleEngine()
        self._permission_checker = PermissionChecker(
            mode=PermissionMode.DEFAULT,
            sandbox=self._sandbox,
            detector=self._detector,
            rule_engine=self._rule_engine,
        )
        self._plan_path: Path | None = None
        self._has_exited_plan_mode: bool = False

        # ── HITL state ──
        self._hitl_event: asyncio.Event = asyncio.Event()
        self._hitl_results: dict[str, bool] = {}  # tool_use_id → allowed

        # ── System prompt builder ──
        model_name = getattr(llm_client.config, "model", "")
        self._prompt_builder = PromptBuilder(
            model=model_name,
            version="0.1.0",
            instructions=instructions,
            memory=memory,
        )

        # ── Skill system ──
        self._skill_catalog: str = ""

    # ── Public API ──────────────────────────────────────────────────

    @property
    def mode(self) -> PlanMode:
        return self._mode

    @property
    def plan_mode(self) -> bool:
        return self._permission_checker.mode == PermissionMode.PLAN

    @property
    def permission_mode(self) -> PermissionMode:
        return self._permission_checker.mode

    def cancel(self) -> None:
        self._cancel.set()

    def set_permission_mode(self, mode: PermissionMode) -> None:
        self._permission_checker.mode = mode
        self._mode = PlanMode.ON if mode == PermissionMode.PLAN else PlanMode.OFF

    def activate_skill(self, name: str, body: str) -> None:
        """激活一个 Skill —— 将 SOP 钉到环境上下文。

        Args:
            name: Skill 名称。
            body: 渲染后的 SOP 正文。
        """
        if self._runtime is not None:
            self._runtime.active_skills.activate(name, body)

    def clear_active_skills(self) -> None:
        """清空全部已激活 Skill。"""
        if self._runtime is not None:
            self._runtime.active_skills.clear()

    def set_skill_catalog(self, catalog: str) -> None:
        """设置 Skill catalog 文本（启动时注入一次）。"""
        self._skill_catalog = catalog

    def resolve_hitl(self, tool_use_id: str, allowed: bool, choice: str = "") -> None:
        """TUI 调用此方法通知 HITL 结果。"""
        self._hitl_results[tool_use_id] = allowed
        if allowed and choice == "allow_session":
            # 记录到会话缓存（由 TUI 传递 content）
            pass
        self._hitl_event.set()

    def toggle_plan_mode(self) -> PlanMode:
        """切换 Plan Mode 开关。"""
        if self.plan_mode:
            # 退出 Plan Mode
            pre_mode = getattr(self, "_pre_plan_permission", PermissionMode.DEFAULT)
            self.set_permission_mode(pre_mode)
        else:
            # 进入 Plan Mode
            self._pre_plan_permission = self.permission_mode
            self.set_permission_mode(PermissionMode.PLAN)
            # 生成 plan 文件路径
            if self._plan_path is None:
                self._plan_path = generate_plan_path(str(self._exec_ctx.cwd))
                self._permission_checker.plan_file_path = str(self._plan_path)
        return self._mode

    async def run(self, user_input: str) -> AsyncGenerator[AgentEvent, None]:
        """运行一次完整的 ReAct 循环。"""
        # ① 显式 toggle 命令
        if user_input.strip().lower() == "/plan":
            new_mode = self.toggle_plan_mode()
            yield AgentFinished(
                text=f"Plan mode: {new_mode.value.upper()}",
                total_usage=self._total_usage,
            )
            return

        # ② 自动检测：用户表达了计划意图 → 自动开启 Plan Mode
        if not self.plan_mode and detect_plan_intent(user_input):
            self.set_permission_mode(PermissionMode.PLAN)
            if self._plan_path is None:
                self._plan_path = generate_plan_path(str(self._exec_ctx.cwd))
                self._permission_checker.plan_file_path = str(self._plan_path)

        self._cancel.clear()
        start_time = time.monotonic()
        self._conversation.add_user_message(user_input)
        self._refresh_memory()  # 笔记更新后下次输入生效

        emergency_retried = False  # 单次 run 内最多一次紧急重试
        iteration = 0
        while iteration < self._config.max_iterations:
            iteration += 1

            # 取消检查（轮次边界）
            if self._cancel.is_set():
                yield AgentError(message="Cancelled", code="cancelled")
                return

            yield IterationUpdate(iteration=iteration, total_usage=self._total_usage)

            # ── Plan Mode: 注入迭代感知 system_reminder ──
            if self.plan_mode and self._plan_path:
                plan_path_str = str(self._plan_path)
                plan_exists = self._plan_path.exists()
                reminder = build_plan_mode_reminder(plan_path_str, plan_exists, iteration)
                self._conversation.add_system_reminder(reminder)

            # ── 上下文压缩：每轮自动两层检查 ──
            if self._runtime is not None:
                async for ev in self._auto_compact_if_needed():
                    yield ev

            # ── 一轮 LLM 流式调用 ──
            api_messages, _ = self._conversation.to_api_format()
            tools = self._build_tool_defs()

            # 构建系统提示（模块化拼装 → 纯字符串）
            env_info = collect_environment(
                work_dir=str(self._exec_ctx.cwd),
                model=self._prompt_builder._model,
                version=self._prompt_builder._version,
            )

            # ── Skill 注入：catalog + active SOP ──
            if self._skill_catalog:
                env_info = self._skill_catalog + "\n\n" + env_info
            if self._runtime is not None:
                active_entries = self._runtime.active_skills.snapshot()
                if active_entries:
                    env_info = env_info + "\n\n" + _render_active_skills_block(active_entries)
            assembly = self._prompt_builder.build_assembly(env_info)
            parts: list[str] = []
            for cb in assembly.cached:
                parts.append(cb.content)
            for ub in assembly.uncached:
                parts.append(ub.content)
            full_system = "\n\n".join(parts)

            stream_msg = self._conversation.start_assistant_stream()
            tool_uses: list[ToolUse] = []
            unknown_count = 0
            error_event: AgentError | None = None

            try:
                async for se in self._client.stream_chat(
                    api_messages, system_prompt=full_system, tools=tools or None,
                ):
                    if self._cancel.is_set():
                        self._conversation.finish_stream(stream_msg)
                        yield AgentError(message="Cancelled", code="cancelled")
                        return

                    if isinstance(se, ThinkingChunk):
                        if se.text:
                            yield ThinkingDelta(text=se.text)

                    elif isinstance(se, TextChunk):
                        self._conversation.append_to_stream(stream_msg, se.text)
                        yield TextDelta(text=se.text)

                    elif isinstance(se, ToolUse):
                        tool_uses.append(se)
                        if not self._registry_has(se.name):
                            unknown_count += 1

                    elif isinstance(se, CompletionDone):
                        self._conversation.finish_stream(stream_msg, se.usage)
                        if se.usage:
                            self._total_usage["input_tokens"] += se.usage.get("input_tokens", 0)
                            self._total_usage["output_tokens"] += se.usage.get("output_tokens", 0)
                            # 更新 usage_anchor（仅主对话路径，摘要请求不经过这里）
                            if self._runtime is not None:
                                self._runtime.usage_anchor = usage_anchor(se.usage)
                                self._runtime.anchor_msg_len = len(
                                    self._conversation.messages
                                )

                    elif isinstance(se, StreamError):
                        self._conversation.fail_stream(stream_msg, se.message)
                        error_event = AgentError(message=se.message, code="stream_error")

            except PromptTooLongError as e:
                # ── 紧急压缩路径 ──
                # PTL 发生时文本不写回 Conversation（保持状态原子）
                self._conversation.fail_stream(stream_msg, str(e))
                if self._runtime is not None and not emergency_retried:
                    yield CompactEvent(phase=CompactPhase.BEFORE_EMERGENCY)
                    emergency_retried, after_ev = await self._emergency_compact(e)
                    if after_ev is not None:
                        yield after_ev
                    if emergency_retried:
                        continue  # 重试本轮 stream（历史已被重建）
                error_event = AgentError(message=str(e), code="ptl_error")

            except Exception as e:
                self._conversation.fail_stream(stream_msg, str(e))
                error_event = AgentError(message=str(e), code="stream_error")

            if error_event:
                yield error_event
                return

            if unknown_count >= self._config.unknown_tool_threshold:
                yield AgentError(
                    message=f"Too many unknown tool requests ({unknown_count})",
                    code="unknown_tool",
                )
                return

            # ── 判断本轮结果 ──
            if not tool_uses:
                elapsed = time.monotonic() - start_time
                self._maybe_trigger_memory(user_input)
                yield AgentFinished(
                    text=self._get_last_assistant_text(),
                    total_usage=self._total_usage,
                    iterations=iteration,
                    elapsed_s=elapsed,
                )
                return

            # ── 执行工具 ──
            async for ev in self._execute_tools(tool_uses):
                yield ev

            # ── 检测 ExitPlanMode → 中断循环 ──
            exit_plan_called = any(tu.name == "ExitPlanMode" for tu in tool_uses)
            if exit_plan_called and self.plan_mode and self._plan_path:
                plan_content = ""
                if self._plan_path.exists():
                    try:
                        plan_content = self._plan_path.read_text(encoding="utf-8")
                    except Exception:
                        pass
                yield PlanReady(
                    plan_path=str(self._plan_path),
                    plan_content=plan_content,
                )
                return

        # 迭代上限
        yield AgentError(
            message=f"Reached iteration limit ({self._config.max_iterations})",
            code="max_iterations",
        )

    # ── Helpers ──────────────────────────────────────────────────────

    def _build_tool_defs(self) -> list[dict[str, Any]]:
        """返回全部工具定义（不过滤，权限检查在执行时进行）。"""
        tools = self._registry.list()
        return [
            {"name": t.name(), "description": t.description(), "input_schema": t.input_schema()}
            for t in tools
        ]

    def _registry_has(self, name: str) -> bool:
        try:
            self._registry.get(name)
            return True
        except Exception:
            return False

    def _get_last_assistant_text(self) -> str:
        for m in reversed(self._conversation.messages):
            if m.role.value == "assistant" and m.content:
                return m.content
        return ""

    # ── 上下文压缩 ───────────────────────────────────────────────────

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
            yield CompactEvent(
                phase=CompactPhase.AFTER_AUTO,
                before=output.before_tokens,
                after=output.after_tokens,
                err=None if layer2_ran else RuntimeError("layer2 未完成"),
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
        retry_est = estimate_tokens(0, self._conversation.messages, 0)
        retryable = retry_est < runtime.context_window - MANUAL_SAFETY_MARGIN
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
        return output.before_tokens, output.after_tokens

    # ── 自动笔记 ────────────────────────────────────────────────────

    def _refresh_memory(self) -> None:
        """笔记更新后刷新记忆索引注入（仅当内容变化，保持缓存稳定）。"""
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
        task = asyncio.create_task(update_memory(provider, store, recent))
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

    # ── Tool execution ───────────────────────────────────────────────

    async def _execute_tools(
        self, tool_uses: list[ToolUse]
    ) -> AsyncGenerator[AgentEvent, None]:
        results: dict[str, tuple[bool, str, int]] = {}

        # 1. 权限预检（对所有工具）
        for tu in tool_uses:
            decision = self._check_tool_permission(tu)
            if decision.effect == "deny":
                results[tu.id] = (False, decision.reason, 0)
            elif decision.effect == "ask":
                yield HITLRequired(
                    tool_name=tu.name,
                    tool_use_id=tu.id,
                    description=self._describe_tool_action(tu),
                    arguments=tu.input,
                    risk_hint=decision.reason,
                )
                self._hitl_event.clear()
                await self._hitl_event.wait()
                allowed = self._hitl_results.get(tu.id, False)
                if allowed:
                    results[tu.id] = await self._exec_one(tu)
                else:
                    results[tu.id] = (False, "User denied", 0)

        # 2. 按类型分批执行已放行的工具
        allowed_tus = [tu for tu in tool_uses if tu.id not in results]
        if allowed_tus:
            batches: list[list[ToolUse]] = []
            for tu in allowed_tus:
                is_read = self._tool_is_readonly(tu.name)
                if batches and is_read == self._tool_is_readonly(batches[-1][0].name):
                    batches[-1].append(tu)
                else:
                    batches.append([tu])

            for batch in batches:
                if self._tool_is_readonly(batch[0].name):
                    for tu in batch:
                        yield ToolCallStarted(tool_use_id=tu.id, name=tu.name, input=tu.input)
                    coros = [self._exec_one(tu) for tu in batch]
                    batch_results = await asyncio.gather(*coros, return_exceptions=True)
                    for tu, raw in zip(batch, batch_results):
                        if isinstance(raw, BaseException):
                            results[tu.id] = (False, str(raw), 0)
                        else:
                            results[tu.id] = raw
                        success, content, duration_ms = results[tu.id]
                        yield ToolCallFinished(
                            tool_use_id=tu.id, name=tu.name, input=tu.input,
                            success=success,
                            result_preview=content[: self._config.result_preview_max_chars],
                            duration_ms=duration_ms,
                        )
                else:
                    for tu in batch:
                        yield ToolCallStarted(tool_use_id=tu.id, name=tu.name, input=tu.input)
                        results[tu.id] = await self._exec_one(tu)
                        success, content, duration_ms = results[tu.id]
                        yield ToolCallFinished(
                            tool_use_id=tu.id, name=tu.name, input=tu.input,
                            success=success,
                            result_preview=content[: self._config.result_preview_max_chars],
                            duration_ms=duration_ms,
                        )

        # 3. 回灌到对话历史（原始顺序）
        for tu in tool_uses:
            self._conversation.add_tool_use(tu.id, tu.name, tu.input)
        for tu in tool_uses:
            success, content, _ = results[tu.id]
            entry = content if success else f"Error: {content}"
            self._conversation.add_tool_result(tu.id, entry)

    def _check_tool_permission(self, tu: ToolUse) -> Decision:
        """检查单个工具调用的权限。"""
        try:
            tool = self._registry.get(tu.name)
            is_read = tool.is_read_only()
            cat = tool.category()
        except Exception:
            is_read = False
            cat = "command"
        if cat in ("read", "file_read", "search", "code_search"):
            tc = "read"
        elif cat in ("write", "file_write", "edit", "plan"):
            tc = "write"
        elif cat in ("shell", "command", "bash"):
            tc = "command"
        elif is_read:
            tc = "read"
        else:
            tc = "command"
        return self._permission_checker.check(tu.name, is_read, tc, tu.input)

    def _describe_tool_action(self, tu: ToolUse) -> str:
        """为 HITL 生成人类可读的操作描述。"""
        content = extract_content(tu.name, tu.input)
        if content:
            return f"[{tu.name}] {content}"
        parts = [f"{k}={str(v)[:80]}" for k, v in tu.input.items()]
        return f"[{tu.name}] {', '.join(parts)}" if parts else tu.name

    async def _exec_one(self, tu: ToolUse) -> tuple[bool, str, int]:
        start = time.monotonic()
        try:
            result = await self._registry.execute(tu.name, self._exec_ctx, tu.input)
            if result.success:
                if tu.name == "read_file" and self._runtime is not None:
                    await self._track_read_file(tu.input)
                ok, content = True, str(result.data or "")
            else:
                ok, content = False, result.error or "Unknown error"
        except asyncio.TimeoutError:
            ok, content = False, f"Tool '{tu.name}' timed out"
        except Exception as e:
            ok, content = False, str(e)
        return ok, content, int((time.monotonic() - start) * 1000)

    async def _track_read_file(self, input: dict[str, Any]) -> None:
        """ReadFile 成功后重读磁盘纯净字节，写入 RecoveryState 供恢复段使用。"""
        runtime = self._runtime
        if runtime is None:
            return
        file_path = input.get("file_path")
        if not file_path:
            return
        p = Path(file_path)
        if not p.is_absolute():
            p = self._exec_ctx.cwd / p
        try:
            data = await asyncio.to_thread(p.read_bytes)
        except OSError:
            return
        runtime.recovery.record_file(
            str(p.resolve()), data.decode("utf-8", errors="replace")
        )

    def _tool_is_readonly(self, name: str) -> bool:
        try:
            return self._registry.get(name).is_read_only()
        except Exception:
            return False


def _render_active_skills_block(entries: list) -> str:
    """将已激活 Skill 的 SOP 渲染为 environment 注入块。

    Args:
        entries: ActiveEntry 列表。

    Returns:
        渲染后的文本，无激活 Skill 时返回空字符串。
    """
    if not entries:
        return ""

    parts = ["## Active Skills"]
    for entry in entries:
        parts.append(f"\n### Skill: {entry.name}\n")
        parts.append(entry.body)
        parts.append("")

    return "\n".join(parts).strip()
