"""Agent 核心 —— ReAct 循环编排。

负责：带工具定义调 LLM → 流式收集 → 执行工具（权限检查） → 结果回灌 → 循环，
直到模型给出不含工具调用的纯文本回复（或触发停止条件）。

集成上下文压缩：每轮自动执行两层检查（Layer1 预防 + Layer2 兜底），
支持手动 /compress 和 PromptTooLongError 紧急压缩。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

from conversation.manager import ConversationManager
from core.agent.agent_compaction import CompactionMixin
from core.agent.agent_permissions import PermissionMixin
from core.agent.agent_state import StateMixin
from core.agent.agent_telemetry import TelemetryMixin
from core.agent.agent_tools import ToolExecutionMixin
from core.agent.config import AgentConfig
from core.agent.events import (
    AgentError,
    AgentEvent,
    AgentFinished,
    CompactEvent,
    CompactPhase,
    IterationUpdate,
    PlanReady,
    TextDelta,
    ThinkingDelta,
)
from core.agent.plan_mode import (
    PlanMode,
    build_plan_mode_reminder,
    detect_plan_intent,
    generate_plan_path,
)
from core.agent.runtime import SessionRuntime
from core.context_compression.token import usage_anchor
from core.host.journal import SideEffectJournal
from core.permissions.checker import PermissionChecker
from core.permissions.dangerous import DangerousCommandDetector
from core.permissions.modes import (
    PermissionMode,
    UnattendedPolicy,
)
from core.permissions.rules import RuleEngine
from core.permissions.sandbox import PathSandbox
from core.prompts.builder import PromptBuilder
from core.prompts.environment import collect_environment
from core.tool.context import ExecutionContext
from core.tool.registry import ToolRegistry
from core.trace.events import (
    AgentEndEvent,
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


class Agent(
    TelemetryMixin,
    CompactionMixin,
    PermissionMixin,
    ToolExecutionMixin,
    StateMixin,
):
    """ReAct 循环编排器。

    方法按关注点拆到 Mixin（各自一个文件），本文件只保留循环编排与状态本身：

    - `TelemetryMixin`（agent_telemetry.py）—— trace / metrics / span
    - `CompactionMixin`（agent_compaction.py）—— 上下文压缩
    - `PermissionMixin`（agent_permissions.py）—— 权限判定与 HITL
    - `ToolExecutionMixin`（agent_tools.py）—— 工具执行
    - `StateMixin`（agent_state.py）—— 会话状态 / 记忆 / hook / 模型切换
    """

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
        journal: SideEffectJournal | None = None,
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
        # 本轮 turn 结束原因（completed/max-tokens/blocked）；max-tokens sticky：
        # 一旦某 step 触顶，本轮最终原因固定为 max-tokens，后续 step 正常完成不降级。
        self._turn_end_reason: str = "completed"
        self._total_usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._runtime = runtime
        # 副作用 journal（可选）：host 模式下记录写/命令类调用，供崩溃恢复判定
        # 「哪些调用可能已经生效」。为空则完全不做记账（默认路径行为不变）。
        self._journal = journal
        # 后台记忆更新任务集合（退出时等待）
        self._memory_tasks: set[asyncio.Task] = set()

        # ── 子 Agent 扩展字段 ──
        self._system_prompt_override = system_prompt
        self._max_turns = max_turns
        self._dont_ask = dont_ask
        # 无人值守策略（host / 无头）：把 `ask` 级决策按策略代答，避免挂死等人。
        # None = 不启用（TUI 等有人值守路径的默认）。
        self._unattended_policy: UnattendedPolicy | None = None
        self._approval_upgrader = approval_upgrader
        # 审批自动审查者（可选）：`REVIEW` 档下由它逐个判断 `ask` 级决策。
        # None = 不启用（其余档位与有人值守路径都不需要它，行为不变）。
        self._approval_reviewer: Any = None

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
        # 加载项目级/用户级权限规则（Layer 3 的数据来源）。
        # 失败不影响启动：拿不到规则等于空规则集，只是回到"无自定义规则"的行为。
        try:
            self._permission_checker.load_rules(exec_ctx.cwd)
        except Exception:  # noqa: BLE001
            pass
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

    def set_unattended_policy(self, policy: UnattendedPolicy | str | None) -> None:
        """设置无人值守策略（host / 无头）。

        设置后 `ask` 级决策由策略代答，不再产生 HITL 事件——无人环境没有
        按键的人，挂在 HITL 上就是挂死。传 None 关闭（回到有人值守语义）。
        """
        self._unattended_policy = (
            UnattendedPolicy(policy) if policy is not None else None
        )

    @property
    def unattended_policy(self) -> UnattendedPolicy | None:
        return self._unattended_policy

    def set_approval_reviewer(self, reviewer: Any) -> None:
        """挂上审批自动审查者（`REVIEW` 档用，见 spec_approval_review）。

        只在 `unattended_policy is REVIEW` 时生效；`None` 表示不启用。
        审查者在 `_execute_tools` 的 ask 分支被调用 —— 它需要**异步**地
        调一次模型，所以不能像其它档位那样在同步的 `_check_tool_permission`
        里代答。
        """
        self._approval_reviewer = reviewer

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
        self._turn_end_reason = "completed"
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
                turn_end_reason=self._turn_end_reason,
            )
            return

        # ② 自动检测：用户表达了计划意图 → 自动开启 Plan Mode
        if not self.plan_mode and detect_plan_intent(user_input):
            self.set_permission_mode(PermissionMode.PLAN)
            if self._plan_path is None:
                self._plan_path = generate_plan_path(str(self._exec_ctx.cwd))
                self._permission_checker.plan_file_path = str(self._plan_path)
            # 埋点：记长度 + 哈希，**不落原文**。
            # 目的 —— 让 `detect_plan_intent` 的真实误判率在几周后可量化：
            # 有了"同一输入重复出现几次""长度分布"就能反推哪些表述在误触发。
            # ★ 刻意不记原文：那等于重演"为量化而落 prompt 原文"，
            #   与 `spec_trace.md` 的既有决策冲突；哈希已足够回答上述问题。
            # 刻意不进 metrics：record_metric 无标签维度，哈希做指标名会造成
            #   高基数（09-26 调研结论「高基数只进 traces/events，不进 metrics」）。
            self._trace_write_dict(
                {
                    "event": "plan_intent_auto",
                    "input_len": len(user_input),
                    "input_sha": hashlib.sha256(user_input.encode("utf-8")).hexdigest()[
                        :12
                    ],
                }
            )

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
                from core.context_compression.token import message_chars
                from core.observability.context import (
                    set_agent_identity,
                    set_iteration_meta,
                )

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

            # 两种互斥形态：`system_prompt`（子 Agent 覆盖）或 `system_blocks`
            # （模块化拼装）。值是 str 或 PromptAssembly —— 显式标注，否则 mypy
            # 从第一个分支推断成 `dict[str, str]`，第二个分支就报 dict-item。
            sys_kwargs: dict[str, Any]

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
                    self._turn_end_reason = "blocked"
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
                        # max-tokens 触顶 → 本轮结束原因固定为 max-tokens（sticky，
                        # 后续 step 正常完成不降级回 completed）
                        if se.stop_reason in ("max_tokens", "length"):
                            self._turn_end_reason = "max-tokens"

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
                    turn_end_reason=self._turn_end_reason,
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

    # ── 自动笔记 ────────────────────────────────────────────────────


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
