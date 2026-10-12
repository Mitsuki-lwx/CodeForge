"""Agent 的工具执行 —— 权限预检之后真正跑工具的那一段。

从 `core/agent/agent.py` 抽出的最大一簇（`_execute_tools` / `_exec_one` /
读文件追踪 / 工具只读与并发安全判定）。它是 ReAct 循环里最长的一段，
单独成文后主循环只剩"调 LLM → 分发事件 → 执行工具"三句话。

执行顺序在这里被**固定死**（顺序本身就是契约，改序会静默改变安全语义）：

  0. `pre_tool` hook 前置关卡（可拦）
  1. 权限预检（deny / ask 分流）
  2. ask 的三条出路，按优先级：冒泡通道 → 审批审查（REVIEW）→ 人工 HITL
  3. 并行安全判定 → 并发或串行执行
  4. 结果回灌对话 + journal + 审计/指标
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from conversation.manager import ConversationManager
from core.agent.config import AgentConfig
from core.agent.events import (
    AgentEvent,
    HITLRequired,
    ToolCallFinished,
    ToolCallStarted,
)
from core.agent.runtime import SessionRuntime
from core.hooks.events import HookContext
from core.host.journal import SIDE_EFFECT_CATEGORIES, SideEffectJournal
from core.permissions.checker import Decision, PermissionChecker
from core.permissions.confidence_gate import gate_on_confidence
from core.permissions.hitl import HITLRequest
from core.permissions.modes import ToolCategory
from core.permissions.reviewer import ReviewContext
from core.permissions.rules import extract_content
from core.tool.context import ExecutionContext
from core.tool.registry import ToolRegistry
from core.trace.events import (
    HookEvent,
    PermissionEvent,
    ToolEndEvent,
    ToolStartEvent,
)
from llm.client import LLMClient
from llm.stream_events import ToolUse


class ToolExecutionMixin:
    """`Agent` 的工具执行与结果回灌。

    ── 对宿主的依赖契约 ──────────────────────────────────────────────
    这是本文件存在的**主要理由**：把"这段代码到底依赖宿主什么"写清楚。
    之前它隐式依赖 Agent 的 13 个字段 + 10 个兄弟 Mixin 的方法，只能靠人肉
    在 1671 行里翻；现在打开文件就能看到全部耦合面。

    纯注解 / `TYPE_CHECKING` 类型桩对运行时**零影响**（注解不创建属性）。
    """

    # ── 由 `Agent.__init__` / 各 setter 赋值的字段 ──
    _registry: ToolRegistry
    _conversation: ConversationManager
    _exec_ctx: ExecutionContext
    _config: AgentConfig
    _client: LLMClient
    _runtime: SessionRuntime | None
    _hooks: Any
    _journal: SideEffectJournal | None
    _permission_checker: PermissionChecker
    _approval_upgrader: Any
    _approval_reviewer: Any
    _agent_name: str
    _hitl_event: asyncio.Event
    _hitl_results: dict[str, bool]

    # ── 由兄弟 Mixin / Agent 提供的方法 ──
    if TYPE_CHECKING:
        # PermissionMixin
        def _tool_category(self, tu: ToolUse) -> tuple[bool, ToolCategory]: ...
        def _should_review_approval(self) -> bool: ...
        def _recent_user_messages(self, limit: int = 3) -> list[str]: ...
        def _check_tool_permission(self, tu: ToolUse) -> Decision: ...
        def _describe_tool_action(self, tu: ToolUse) -> str: ...

        # StateMixin
        def _hook_ctx(self, event: str, **extra: Any) -> HookContext: ...
        def _emit_hook(self, event: str, ctx: HookContext) -> None: ...

        # TelemetryMixin
        def _trace_record(self, event: Any) -> None: ...
        def _emit_metric(
            self, name: str, value: float, *, delta: bool = True
        ) -> None: ...
        def _emit_tool_span(
            self,
            tool_use_id: str,
            tool_name: str,
            success: bool,
            duration_ms: int,
            retries: int = 0,
            timed_out: bool = False,
        ) -> None: ...

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
            elif decision.effect == "ask" and self._approval_upgrader is not None:
                # ── 升级到上层决策（spec 能力清单第 9 条的第三层）──
                # 子 Agent 在 `_run_loop` 里**内部消费**自己的事件流，它 yield 出来的
                # `HITLRequired` 无人读；而父 Agent 此刻正处于 `await`、不能 yield。
                # 所以这里不走事件流，直接等一条独立通道。形态选择的实测对比见
                # `core/permissions/upgrade.py` 模块 docstring。
                outcome = await self._approval_upgrader.request(
                    HITLRequest(
                        tool_name=tu.name,
                        description=self._describe_tool_action(tu),
                        arguments=tu.input,
                        risk_hint=decision.reason,
                        origin=self._agent_name,
                    )
                )
                if outcome.allowed:
                    # 「本会话允许」必须记到**子 Agent 自己的**权限账本上。
                    # 记到主 Agent 的会让"为子 Agent 放行"变成"给主 Agent 也放行"
                    # —— 那是反向越权，正好是本通道要防的东西。
                    if outcome.choice == "allow_session":
                        self._permission_checker.add_session_allow(
                            tu.name, extract_content(tu.name, tu.input)
                        )
                    results[tu.id] = await self._exec_one(tu)
                else:
                    # 原因必须可读：父要靠它向用户解释"这步为什么没做成"。
                    results[tu.id] = (False, outcome.reason, 0, {})
            elif decision.effect == "ask" and self._should_review_approval():
                # ── 审批自动审查（`REVIEW` 档，见 spec_approval_review）──
                # 顺序要紧：**能问人时优先问人**（人的判断最准），只有确实没人可问
                # 才轮到审查者 —— 所以这条必须排在"冒泡"之后、人工 HITL 之前。
                # （第一版把两条写反了，探针 `verify_review_seam.py` 用例 C 抓到：
                #   有冒泡通道时审查者仍被调用。）
                # 审查是独立的一次模型调用，任何失败都 fail-closed（reviewer 内兜底）。
                # 类别要在这里现算：`_execute_tools` 没有 `_check_tool_permission`
                # 里的那个 `tc`（审查需要它的粒度来提示风险）。
                _is_read, tc = self._tool_category(tu)
                outcome = await self._approval_reviewer.review(
                    ReviewContext(
                        tool_name=tu.name,
                        tool_input=tu.input,
                        category=tc,
                        permission_reason=decision.reason,
                        cwd=str(self._exec_ctx.cwd),
                        user_intent=self._recent_user_messages(),
                    )
                )
                # 置信度闸门（spec_confidence_gate）：审查说"放行"时，再看把握够不够。
                # 不提供置信度的后端（现有 LLM 后端）→ 原样放行，行为逐字不变。
                allow, gate_reason = gate_on_confidence(outcome)
                if allow:
                    results[tu.id] = await self._exec_one(tu)
                else:
                    # 闸门的原因更具体（含实际置信度与所用阈值），优先用它；
                    # 没有闸门原因时回落到审查者的（含"拒绝理由"与"没跑起来"的可区分文案）。
                    results[tu.id] = (False, gate_reason or outcome.reason, 0, {})
            elif decision.effect == "ask":
                # 顺序要紧：**先 clear 再 yield**。
                # yield 把控制权交给调用方，而调用方可能在同一轮事件循环里立刻
                # 调 resolve_hitl（host 无人值守路径就是自动拒绝，无人工延迟）。
                # 若 clear 放在 yield 之后，生成器恢复时会把这期间刚 set 的事件
                # 重新清掉，随后的 wait() 永远等不到 → 回合静默挂死。
                # TUI 路径因为有人工操作延迟，set 总发生在 wait() 之后，恰好
                # 绕过了这个竞态，所以问题只在无人值守场景暴露。
                self._hitl_event.clear()
                yield HITLRequired(
                    tool_name=tu.name,
                    tool_use_id=tu.id,
                    description=self._describe_tool_action(tu),
                    arguments=tu.input,
                    risk_hint=decision.reason,
                    origin=self._agent_name,
                )
                await self._hitl_event.wait()
                allowed = self._hitl_results.get(tu.id, False)
                if allowed:
                    results[tu.id] = await self._exec_one(tu)
                else:
                    # 交互式路径下的拒绝只有一种来源（用户点了拒绝），
                    # 但仍写清"交互确认"，与策略代答 / 审批超时区分开。
                    results[tu.id] = (False, "用户拒绝了该调用（交互确认）", 0, {})

        # 2. 按并发安全性分批执行已放行的工具
        allowed_tus = [tu for tu in tool_uses if tu.id not in results]
        if allowed_tus:
            batches: list[list[ToolUse]] = []
            for tu in allowed_tus:
                safe = self._tool_is_parallel_safe(tu)
                if batches and safe == self._tool_is_parallel_safe(batches[-1][0]):
                    batches[-1].append(tu)
                else:
                    batches.append([tu])

            for batch in batches:
                if self._tool_is_parallel_safe(batch[0]):
                    for tu in batch:
                        self._trace_record(
                            ToolStartEvent(tool_use_id=tu.id, tool_name=tu.name)
                        )
                        yield ToolCallStarted(
                            tool_use_id=tu.id, name=tu.name, input=tu.input
                        )
                    coros = [self._exec_one(tu) for tu in batch]
                    batch_results = await asyncio.gather(*coros, return_exceptions=True)
                    # gather 保证等长 —— strict=True 把这个不变量写死
                    for tu, raw in zip(batch, batch_results, strict=True):
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
            success, content, _, tool_meta = results[tu.id]
            entry = content if success else f"Error: {content}"
            images = tool_meta.get("_images") or []
            if images and not self._vision_enabled():
                # 模型看不了图：图像不回灌，但**要说清楚**截图还在磁盘上，
                # 否则模型会以为截图失败了。人也照样能看到那张 PNG。
                entry += (
                    f"\n\n（本次附带 {len(images)} 张图像，但当前模型未开启 vision，"
                    "未回灌上下文；文件已落盘，路径见上。）"
                )
                images = []
            self._conversation.add_tool_result(tu.id, entry, images)

    async def _exec_one(self, tu: ToolUse) -> tuple[bool, str, int, dict]:
        start = time.monotonic()
        tool_meta: dict = {}
        try:
            result = await self._registry.execute(tu.name, self._exec_ctx, tu.input)
            tool_meta = dict(result.meta or {})
            # 图像附件走私有键传回调用方：`meta` 只被读 `retries` / `timed_out`，
            # 不会整体进 trace / 日志，所以 base64 不会污染审计文件。
            if result.images:
                tool_meta["_images"] = result.images
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
        # 副作用记账：工具已经成功执行、工作区可能已被改动，立刻落盘。
        # 刻意放在 post_tool 钩子之前——钩子抛错不能把「已经改过工作区」这件事吞掉。
        # journal 关闭时不做任何额外工作（连类别都不查），默认路径零开销。
        if ok and self._journal is not None:
            _is_read, category = self._tool_category(tu)
            if category in SIDE_EFFECT_CATEGORIES:
                self._journal.record(
                    tool_use_id=tu.id,
                    tool_name=tu.name,
                    category=category,
                    result=content,
                )
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

    def _vision_enabled(self) -> bool:
        """当前模型是否支持图像输入（`provider.vision`，默认 False）。"""
        try:
            return bool(getattr(self._client.config, "vision", False))
        except Exception:  # noqa: BLE001 —— 拿不到配置就当不支持（保守）
            return False

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

    async def run_to_completion(
        self,
        conv: ConversationManager,
        task: str = "",
        events: asyncio.Queue | None = None,
    ) -> str:
        """跑一轮完整会话直到收敛，返回最终文本。

        子 Agent / 后台任务 / 基准 harness 的入口。

        此前这个方法是在 `core/agent/__init__.py` 里用 `attach_to_agent()`
        **运行时挂到 `Agent` 类上**的：读 `agent.py` 看不到它、grep 不到定义、
        类型检查也看不见，只有"知道有这回事"的人才能在 `sub_agent.py` 里找到它。
        现在改成显式方法，实现仍是 `sub_agent.run_to_completion`（延迟 import
        打破 agent ↔ sub_agent 的循环，也保证测试对
        `core.agent.sub_agent.run_to_completion` 的打桩依然生效）。
        """
        from core.agent.sub_agent import run_to_completion

        return await run_to_completion(self, conv, task, events)

    def _tool_is_readonly(self, name: str) -> bool:
        try:
            return self._registry.get(name).is_read_only()
        except Exception:
            return False

    def _tool_is_parallel_safe(self, tu: ToolUse) -> bool:
        """工具是否可并发执行：只读 OR 声明并发安全（如隔离/只读子代理）。

        Agent 工具对隔离（worktree）或只读子代理返回并发安全，共享工作区
        可写子代理返回不安全（会互相改文件冲突），从而进入串行分支。
        """
        try:
            tool = self._registry.get(tu.name)
        except Exception:
            return False
        if tool.is_read_only():
            return True
        return tool.is_concurrency_safe(tu.input)
