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
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

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
from core.hooks.events import HookContext
from core.permissions.checker import Decision, PermissionChecker
from core.permissions.dangerous import DangerousCommandDetector
from core.permissions.modes import PermissionMode
from core.permissions.rules import RuleEngine, extract_content
from core.permissions.sandbox import PathSandbox
from core.prompts.builder import PromptBuilder
from core.prompts.environment import collect_environment
from core.tool.context import ExecutionContext
from core.tool.registry import ToolRegistry
from core.trace.events import (
    AgentEndEvent,
    AgentErrorEvent,
    HookEvent,
    PermissionEvent,
    ToolEndEvent,
    ToolStartEvent,
)
from core.trace.events import (
    CompactEvent as TraceCompactEvent,
)
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
        hooks: object | None = None,
        # ── 子 Agent 扩展参数 ──
        system_prompt: str | None = None,
        max_turns: int = 0,
        permission_mode: PermissionMode | None = None,
        dont_ask: bool = False,
        approval_upgrader: object | None = None,
        trace_audit_dir: str | None = None,
        loop: object | None = None,  # AgentLoop | None（spec_loop；默认 ReactLoop）
    ) -> None:
        self._registry = registry
        self._client = llm_client
        self._exec_ctx = exec_ctx
        self._conversation = conversation
        self._config = config or AgentConfig()
        self._mode: PlanMode = PlanMode.OFF
        self._cancel: asyncio.Event = asyncio.Event()
        # 运行阶段状态机（idle/running；参考 deepseek-harness phase）。
        # 供治理/观测准确判断「空闲 vs 运行中」，cancel 语义区分的基础。
        self._phase: str = "idle"
        self._total_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._runtime = runtime
        # 后台记忆更新任务集合（退出时等待）
        self._memory_tasks: set[asyncio.Task] = set()

        # ── 子 Agent 扩展字段 ──
        self._system_prompt_override = system_prompt
        self._max_turns = max_turns
        self._dont_ask = dont_ask
        self._approval_upgrader = approval_upgrader

        # ── Permission system ──
        self._sandbox = PathSandbox(work_dir=str(exec_ctx.cwd))
        self._detector = DangerousCommandDetector()
        self._rule_engine = RuleEngine()
        _init_mode = (
            permission_mode if permission_mode is not None else PermissionMode.DEFAULT
        )
        self._permission_checker = PermissionChecker(
            mode=_init_mode,
            sandbox=self._sandbox,
            detector=self._detector,
            rule_engine=self._rule_engine,
        )
        self._plan_path: Path | None = None
        self._has_exited_plan_mode: bool = False

        # ── 试验/评测强制拒绝集：命中即 deny（默认空，benchmark edges 注入）──
        self._deny_tools: set[str] = set()

        # ── 可读身份名（子 Agent 委派时由 Agent 工具 name/role 注入），span 归属用 ──
        self._agent_name: str = ""

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

        # ── Hook system ──
        self._hooks = hooks
        self._turn_finished = False  # 单轮 run 内 turn_end 只触发一次

        # ── Trace (可观测性审计):惰性建 writer,首条事件产生时才开 audit 文件 ──
        self._trace_writer: object | None = None  # core.trace.TraceWriter | None
        self._trace_audit_dir = trace_audit_dir  # 可覆盖 audit 根目录(测试/部署)

        # ── Agent 循环插件化（spec_loop）：默认 ReactLoop（现有行为） ──
        from core.agent.loop import ReactLoop

        self._loop = loop if loop is not None else ReactLoop()

    def _get_trace_writer(self):
        """惰性取审计写入器;失败返回 None(采集失败绝不阻断主流程)。"""
        if self._trace_writer is None:
            try:
                from core.trace.writer import TraceWriter

                self._trace_writer = TraceWriter(
                    self._exec_ctx.session_id, audit_dir=self._trace_audit_dir
                )
            except Exception:  # noqa: BLE001 —— trace 初始化失败静默降级
                self._trace_writer = None
        return self._trace_writer

    def _trace_record(self, event) -> None:
        """发一条审计事件;writer 为空或写失败都静默跳过。"""
        writer = self._get_trace_writer()
        if writer is not None:
            try:
                writer.record(event)
            except Exception:  # noqa: BLE001 —— 审计失败绝不抛出
                pass

    def _trace_close(self) -> None:
        """关闭审计写入器(幂等);未创建过 writer 或已关闭都静默。"""
        if self._trace_writer is not None:
            try:
                self._trace_writer.close()
            except Exception:  # noqa: BLE001 —— 关闭失败绝不抛出
                pass

    def _emit_metric(self, name: str, value: float, *, delta: bool = True) -> None:
        """发一个进程内指标(可观测性);未启用/失败静默。"""
        try:
            from core.observability.providers import record_metric

            record_metric(name, value, delta=delta)
        except Exception:  # noqa: BLE001 —— 可观测性失败绝不抛出
            pass

    def _emit_tool_span(
        self,
        tool_use_id: str,
        tool_name: str,
        success: bool,
        duration_ms: int,
        retries: int = 0,
        timed_out: bool = False,
    ) -> None:
        """为一次工具执行发一个 OTel span(尽力而为;失败静默)。

        用 tool_use_id 作 span 名,标记 tool_name/success/duration/retries/超时;
        失败时把 span.status 置为 ERROR(便于在 trace 树里一眼看出红点)。
        不嵌套父级(并发只读工具下父子关系由 trace JSONL 的 parent_span_id 承担)。
        """
        try:
            from core.observability.providers import get_tracer
            from llm.llm_span import trace_status_error

            tracer = get_tracer("codeforge.tools")
            start = getattr(tracer, "start_as_current_span", None)
            if start is None:
                return  # 可观测性未启用,直接跳过
            outer = start(f"tool.{tool_name}")
            span = outer.__enter__()
            try:
                if hasattr(span, "set_attribute"):
                    span.set_attribute("tool_use_id", tool_use_id)
                    span.set_attribute("success", success)
                    span.set_attribute("duration_ms", duration_ms)
                    span.set_attribute("codeforge.tool.retries", retries)
                    span.set_attribute("codeforge.tool.timeout", timed_out)
                    _stamp_agent_attrs(span, self)
                if not success and hasattr(span, "set_status"):
                    span.set_status(trace_status_error(), None)
            finally:
                outer.__exit__(None, None, None)
        except Exception:  # noqa: BLE001 —— 可观测性失败绝不抛出
            pass

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

    @property
    def max_turns(self) -> int:
        """子 Agent 最大迭代轮数；0 表示用 config 默认值。"""
        return self._max_turns if self._max_turns > 0 else self._config.max_iterations

    @property
    def dont_ask(self) -> bool:
        """子 Agent 是否启用 dontAsk 模式。"""
        return self._dont_ask

    @property
    def running(self) -> bool:
        """是否处于运行中（run 生命周期内）。供治理/观测准确判断。"""
        return self._phase == "running"

    def set_phase(self, phase: str) -> None:
        """显式设置运行阶段（idle/running）。"""
        self._phase = phase

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

    def append_system_prompt(self, suffix: str) -> None:
        """在现有 system prompt 后追加一段（Coordinator 纪律提示词等）。"""
        base = self._system_prompt_override or ""
        self._system_prompt_override = (base + "\n\n" + suffix) if suffix else base

    def set_allowed_tools(self, allowed: list[str]) -> None:
        """收窄模型可见的工具集（Coordinator 模式剥夺写类工具）。

        仅影响 `_build_tool_defs` 呈现给模型的工具列表；执行时的权限由
        permission checker 兜底（仍会拒绝被剥夺工具的实际调用）。
        """
        self._allowed_tools_override: list[str] | None = list(allowed)

    def set_deny_tools(self, tools: list[str]) -> None:
        """强制拒绝执行指定工具（命中即 deny）。

        试验/评测路径用：注入一个工具名集，`_check_tool_permission` 对这些工具
        直接返回 deny → 触发 `codeforge.permission.denied` 指标与工具失败。
        默认空：不影响正常语义。
        """
        self._deny_tools = set(tools)

    def set_agent_name(self, name: str) -> None:
        """设置可读身份名（子 Agent 委派时注入），span 归属 `codeforge.agent.name` 用。"""
        self._agent_name = name or ""

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
        """运行一次完整的 ReAct 循环（经 loop 策略，spec_loop）。"""
        self._turn_finished = False
        self._phase = "running"
        try:
            async for ev in self._loop.run(self, self._conversation, user_input):
                yield ev
        finally:
            # T9: 轮次结束统一关闭审计写入器(天然完成/错误/取消/异常都走这里)
            self._trace_close()
            self._phase = "idle"

    async def _run_loop(self, user_input: str) -> AsyncGenerator[AgentEvent, None]:
        """ReAct 主循环(被 run 包裹以统一收尾)。"""
        # ── Hook: 轮次开始（每次 run 入口）──
        if self._hooks is not None:
            self._emit_hook(
                "turn_start", self._hook_ctx("turn_start", user_input=user_input)
            )

        # ① 显式 toggle 命令
        if user_input.strip().lower() == "/plan":
            new_mode = self.toggle_plan_mode()
            self._finish_turn(user_input, None)
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
        # ── 消费 pending_reminders（后台任务通知等）──
        if self._runtime is not None and self._runtime.pending_reminders:
            for reminder in self._runtime.pending_reminders:
                self._conversation.add_system_reminder(reminder)
            self._runtime.pending_reminders.clear()
        self._conversation.add_user_message(user_input)
        self._refresh_memory()  # 笔记更新后下次输入生效
        if self._hooks is not None:
            self._emit_hook(
                "user_message", self._hook_ctx("user_message", content=user_input)
            )

        emergency_retried = False  # 单次 run 内最多一次紧急重试
        iteration = 0
        while iteration < self.max_turns:
            iteration += 1

            # 取消检查（轮次边界）
            if self._cancel.is_set():
                self._finish_turn(user_input, "cancelled")
                yield AgentError(message="Cancelled", code="cancelled")
                return

            yield IterationUpdate(iteration=iteration, total_usage=self._total_usage)

            # ── Plan Mode: 注入迭代感知 system_reminder ──
            if self.plan_mode and self._plan_path:
                plan_path_str = str(self._plan_path)
                plan_exists = self._plan_path.exists()
                reminder = build_plan_mode_reminder(
                    plan_path_str, plan_exists, iteration
                )
                self._conversation.add_system_reminder(reminder)

            # ── 上下文压缩：每轮自动两层检查 ──
            if self._runtime is not None:
                async for ev in self._auto_compact_if_needed():
                    yield ev

            # ── 一轮 LLM 流式调用 ──
            api_messages, _ = self._conversation.to_api_format()
            # 迭代级诊断：把「迭代号 + 喂入 payload 字符数」写进本轮 LLM span（纯计算，no-op 安全）
            try:
                from core.observability.context import (
                    set_agent_identity,
                    set_iteration_meta,
                )
                from core.context_compression.token import message_chars

                set_iteration_meta(iteration, message_chars(api_messages))
                # 主 Agent 身份：id=session_id，name='lead'（与子 agent 区分）
                set_agent_identity(
                    str(self._exec_ctx.session_id), self._agent_name or "lead"
                )
            except Exception:  # noqa: BLE001 —— 观测辅助失败静默
                pass
            tools = self._build_tool_defs()

            # 构建系统提示（模块化拼装 → 纯字符串）
            env_info = collect_environment(
                work_dir=str(self._exec_ctx.cwd),
                model=self._prompt_builder._model,
                version=self._prompt_builder._version,
            )

            # ── 子 Agent 系统提示覆盖：跳过模块化拼装 ──
            if self._system_prompt_override is not None:
                full_system = self._system_prompt_override
                if env_info.strip():
                    full_system = full_system + "\n\n" + env_info.strip()
                sys_kwargs = {"system_prompt": full_system}
            else:
                # ── Skill 注入：catalog + active SOP ──
                if self._skill_catalog:
                    env_info = self._skill_catalog + "\n\n" + env_info
                if self._runtime is not None:
                    active_entries = self._runtime.active_skills.snapshot()
                    if active_entries:
                        env_info = (
                            env_info
                            + "\n\n"
                            + _render_active_skills_block(active_entries)
                        )
                if self._hooks is not None:
                    injections = self._hooks.inject_store().snapshot()
                    if injections:
                        env_info = (
                            env_info + "\n\n" + _render_hook_injections(injections)
                        )
                assembly = self._prompt_builder.build_assembly(env_info)
                # 稳定块（cached）与变化块（uncached）分离，走 system_blocks 让
                # adapter 打 cache_control 断点命中前缀缓存（省钱/降首字延迟）。
                sys_kwargs = {"system_blocks": assembly}

            # ── pre_step 钩子：每轮 LLM 前拦截（治理/限流/预算），blocked → 停止本轮 ──
            if self._hooks is not None:
                try:
                    _blocked, _reason = await self._hooks.check_pre_step(
                        self._hook_ctx("pre_step", iteration=iteration)
                    )
                except Exception:  # noqa: BLE001 —— hook 故障 fail-open，不误伤正常轮
                    _blocked = False
                if _blocked:
                    self._finish_turn(user_input, "pre_step_blocked")
                    yield AgentError(
                        message=_reason or "pre_step blocked",
                        code="pre_step_blocked",
                    )
                    return

            stream_msg = self._conversation.start_assistant_stream()
            tool_uses: list[ToolUse] = []
            unknown_count = 0
            error_event: AgentError | None = None

            try:
                async for se in self._client.stream_chat(
                    api_messages,
                    tools=tools or None,
                    **sys_kwargs,
                ):
                    if self._cancel.is_set():
                        self._conversation.finish_stream(stream_msg)
                        self._finish_turn(user_input, "cancelled")
                        yield AgentError(message="Cancelled", code="cancelled")
                        return

                    if isinstance(se, ThinkingChunk):
                        if se.text:
                            self._conversation.append_reasoning(stream_msg, se.text)
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
                            self._total_usage["input_tokens"] += se.usage.get(
                                "input_tokens", 0
                            )
                            self._total_usage["output_tokens"] += se.usage.get(
                                "output_tokens", 0
                            )
                            # 更新 usage_anchor（仅主对话路径，摘要请求不经过这里）
                            if self._runtime is not None:
                                self._runtime.usage_anchor = usage_anchor(se.usage)
                                self._runtime.anchor_msg_len = len(
                                    self._conversation.messages
                                )

                    elif isinstance(se, StreamError):
                        self._conversation.fail_stream(stream_msg, se.message)
                        error_event = AgentError(
                            message=se.message, code="stream_error"
                        )

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
                self._finish_turn(user_input, error_event.code)
                yield error_event
                return

            if unknown_count >= self._config.unknown_tool_threshold:
                self._finish_turn(user_input, "unknown_tool")
                yield AgentError(
                    message=f"Too many unknown tool requests ({unknown_count})",
                    code="unknown_tool",
                )
                return

            # ── 判断本轮结果 ──
            if not tool_uses:
                if self._hooks is not None:
                    self._emit_hook(
                        "assistant_message",
                        self._hook_ctx(
                            "assistant_message",
                            content=self._get_last_assistant_text(),
                        ),
                    )
                self._finish_turn(user_input, None)
                elapsed = time.monotonic() - start_time
                # T7: 自然完成 → 审计 agent_end(耗时 + 总用量)
                self._trace_record(
                    AgentEndEvent(
                        elapsed_s=elapsed,
                        token=dict(self._total_usage),
                    )
                )
                # 指标:本轮 token 总量(增量累加到进程内快照时为整体,故用设置值)
                self._emit_metric(
                    "codeforge.tokens.input",
                    self._total_usage.get("input_tokens", 0),
                    delta=False,
                )
                self._emit_metric(
                    "codeforge.tokens.output",
                    self._total_usage.get("output_tokens", 0),
                    delta=False,
                )
                self._emit_metric("codeforge.agent.elapsed_s", elapsed, delta=False)
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
                self._finish_turn(user_input, None)
                return

        # 迭代上限
        self._finish_turn(user_input, "max_iterations")
        yield AgentError(
            message=f"Reached iteration limit ({self.max_turns})",
            code="max_iterations",
        )

    # ── Helpers ──────────────────────────────────────────────────────

    def _build_tool_defs(self) -> list[dict[str, Any]]:
        """返回全部工具定义（不过滤，权限检查在执行时进行）。

        若设置了 `_allowed_tools_override`（Coordinator 模式），只返回白名单内的工具。
        """
        allowed_override = getattr(self, "_allowed_tools_override", None)
        tools = self._registry.list()
        if allowed_override is not None:
            allowed_set = set(allowed_override)
            tools = [t for t in tools if t.name() in allowed_set]
        return [
            {
                "name": t.name(),
                "description": t.description(),
                "input_schema": t.input_schema(),
            }
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

    # ── 自动笔记 ────────────────────────────────────────────────────

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

    async def _execute_tools(
        self, tool_uses: list[ToolUse]
    ) -> AsyncGenerator[AgentEvent, None]:
        from core.observability.providers import record_histogram

        # results[id] = (ok, content, duration_ms, tool_meta)
        results: dict[str, tuple[bool, str, int, dict]] = {}

        # 0. pre_tool hook 前置关卡（位于权限预检之前）
        if self._hooks is not None:
            for tu in tool_uses:
                blocked, reason = await self._hooks.check_pre_tool(
                    self._hook_ctx("pre_tool", tool_name=tu.name, input=tu.input)
                )
                # T4: hook 拦截审计(含放行态,记录本次 pre_tool 判定)
                self._trace_record(
                    HookEvent(
                        tool_name=tu.name,
                        blocked=blocked,
                        reason=reason if blocked else "",
                    )
                )
                if blocked:
                    results[tu.id] = (False, f"Blocked by hook: {reason}", 0, {})

        # 1. 权限预检（对所有工具）
        for tu in tool_uses:
            decision = self._check_tool_permission(tu)
            # T4: 权限决策审计(放行/拒绝/询问 + 理由)
            self._trace_record(
                PermissionEvent(
                    tool_name=tu.name,
                    tool_use_id=tu.id,
                    decision=str(decision.effect),
                    reason=decision.reason,
                )
            )
            if str(decision.effect) == "deny":
                self._emit_metric("codeforge.permission.denied", 1, delta=True)
            if decision.effect == "deny":
                results[tu.id] = (False, decision.reason, 0, {})
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
                    results[tu.id] = (False, "User denied", 0, {})

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
                        self._trace_record(
                            ToolStartEvent(tool_use_id=tu.id, tool_name=tu.name)
                        )
                        yield ToolCallStarted(
                            tool_use_id=tu.id, name=tu.name, input=tu.input
                        )
                    coros = [self._exec_one(tu) for tu in batch]
                    batch_results = await asyncio.gather(*coros, return_exceptions=True)
                    for tu, raw in zip(batch, batch_results):
                        if isinstance(raw, BaseException):
                            results[tu.id] = (False, str(raw), 0, {})
                        else:
                            results[tu.id] = raw
                        success, content, duration_ms, tool_meta = results[tu.id]
                        self._trace_record(
                            ToolEndEvent(
                                tool_use_id=tu.id,
                                tool_name=tu.name,
                                success=success,
                                duration_ms=duration_ms,
                                result_preview=content[
                                    : self._config.result_preview_max_chars
                                ],
                            )
                        )
                        self._emit_metric("codeforge.tool.calls", 1, delta=True)
                        self._emit_metric(
                            "codeforge.tool.duration_ms", duration_ms, delta=True
                        )
                        self._emit_tool_span(
                            tu.id,
                            tu.name,
                            success,
                            duration_ms,
                            retries=tool_meta.get("retries", 0),
                            timed_out=tool_meta.get("timed_out", False),
                        )
                        record_histogram(
                            "codeforge.tool.duration_ms", duration_ms, unit="ms"
                        )
                        yield ToolCallFinished(
                            tool_use_id=tu.id,
                            name=tu.name,
                            input=tu.input,
                            success=success,
                            result_preview=content[
                                : self._config.result_preview_max_chars
                            ],
                            duration_ms=duration_ms,
                        )
                else:
                    for tu in batch:
                        self._trace_record(
                            ToolStartEvent(tool_use_id=tu.id, tool_name=tu.name)
                        )
                        yield ToolCallStarted(
                            tool_use_id=tu.id, name=tu.name, input=tu.input
                        )
                        results[tu.id] = await self._exec_one(tu)
                        success, content, duration_ms, tool_meta = results[tu.id]
                        self._trace_record(
                            ToolEndEvent(
                                tool_use_id=tu.id,
                                tool_name=tu.name,
                                success=success,
                                duration_ms=duration_ms,
                                result_preview=content[
                                    : self._config.result_preview_max_chars
                                ],
                            )
                        )
                        self._emit_metric("codeforge.tool.calls", 1, delta=True)
                        self._emit_metric(
                            "codeforge.tool.duration_ms", duration_ms, delta=True
                        )
                        self._emit_tool_span(
                            tu.id,
                            tu.name,
                            success,
                            duration_ms,
                            retries=tool_meta.get("retries", 0),
                            timed_out=tool_meta.get("timed_out", False),
                        )
                        record_histogram(
                            "codeforge.tool.duration_ms", duration_ms, unit="ms"
                        )
                        yield ToolCallFinished(
                            tool_use_id=tu.id,
                            name=tu.name,
                            input=tu.input,
                            success=success,
                            result_preview=content[
                                : self._config.result_preview_max_chars
                            ],
                            duration_ms=duration_ms,
                        )

        # 3. 回灌到对话历史（原始顺序）
        for tu in tool_uses:
            self._conversation.add_tool_use(tu.id, tu.name, tu.input)
        for tu in tool_uses:
            success, content, _, _ = results[tu.id]
            entry = content if success else f"Error: {content}"
            self._conversation.add_tool_result(tu.id, entry)

    def _check_tool_permission(self, tu: ToolUse) -> Decision:
        """检查单个工具调用的权限。"""
        # 强制拒绝集：命中即 deny（试验/评测注入）
        if tu.name in getattr(self, "_deny_tools", set()):
            return Decision(effect="deny", reason="tool denied by forced deny_tools")
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
        decision = self._permission_checker.check(tu.name, is_read, tc, tu.input)
        # ── 子 Agent dontAsk 模式：Ask 自动转 Allow ──
        if self._dont_ask and decision.effect == "ask":
            return Decision(effect="allow", reason="dontAsk: auto-approved")
        return decision

    def _describe_tool_action(self, tu: ToolUse) -> str:
        """为 HITL 生成人类可读的操作描述。"""
        content = extract_content(tu.name, tu.input)
        if content:
            return f"[{tu.name}] {content}"
        parts = [f"{k}={str(v)[:80]}" for k, v in tu.input.items()]
        return f"[{tu.name}] {', '.join(parts)}" if parts else tu.name

    async def _exec_one(self, tu: ToolUse) -> tuple[bool, str, int, dict]:
        start = time.monotonic()
        tool_meta: dict = {}
        try:
            result = await self._registry.execute(tu.name, self._exec_ctx, tu.input)
            tool_meta = dict(result.meta or {})
            if result.success:
                if tu.name == "read_file" and self._runtime is not None:
                    await self._track_read_file(tu.input)
                ok, content = True, str(result.data or "")
            else:
                ok, content = False, result.error or "Unknown error"
        except TimeoutError:
            ok, content = False, f"Tool '{tu.name}' timed out"
            tool_meta["timed_out"] = True
        except Exception as e:
            ok, content = False, str(e)
        if self._hooks is not None:
            self._emit_hook(
                "post_tool",
                self._hook_ctx(
                    "post_tool",
                    tool_name=tu.name,
                    input=tu.input,
                    tool_result=content[: self._config.result_preview_max_chars],
                    is_error=not ok,
                ),
            )
        return ok, content, int((time.monotonic() - start) * 1000), tool_meta

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


def _stamp_agent_attrs(span: Any, agent: Agent) -> None:
    """给工具 span 打当前 agent 身份（id + 可读名），供在 Langfuse 按 subagent 聚合。

    优先读 ContextVar（LLM 循环设的）；兜底用 agent 自身字段。尽力而为，绝不抛。
    """
    try:
        from core.observability.context import get_agent_identity

        identity = get_agent_identity()
        if identity is not None:
            agent_id, agent_name = identity
        else:
            agent_id = str(agent._exec_ctx.session_id)
            agent_name = getattr(agent, "_agent_name", "") or "lead"
        if hasattr(span, "set_attribute"):
            span.set_attribute("codeforge.agent.id", agent_id or "?")
            if agent_name:
                span.set_attribute("codeforge.agent.name", agent_name)
    except Exception:  # noqa: BLE001 —— 观测辅助失败静默
        return


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


def _render_hook_injections(injections: list[str]) -> str:
    """将 Hook prompt 动作注入的文本渲染为 environment 注入块。

    Returns:
        渲染后的文本，无注入时返回空字符串。
    """
    if not injections:
        return ""

    parts = ["## Hook Injections"]
    for text in injections:
        parts.append(text)
        parts.append("")

    return "\n".join(parts).strip()
