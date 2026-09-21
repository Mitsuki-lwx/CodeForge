"""Skill 执行器 —— inline / fork 分发与工具白名单过滤。

Execute inline: SOP 注入主对话，共享 Agent 上下文。
Execute fork: 独立子会话执行，结果回流到主对话。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from core.skills.errors import SkillDependencyError
from core.skills.render import render_body
from core.tool.registry import ToolRegistry

if TYPE_CHECKING:
    from conversation.manager import ConversationManager
    from core.commands.ui import UI
    from core.skills.loader import SkillLoader

logger = logging.getLogger(__name__)

# 系统工具名称集合 —— 这些工具在 Skill 白名单过滤时自动透传
SYSTEM_TOOL_NAMES: frozenset[str] = frozenset({"LoadSkill"})


def filter_tool_registry(
    registry: ToolRegistry,
    allowed: list[str],
    skill_name: str = "",
) -> ToolRegistry:
    """按白名单过滤工具注册表，系统工具自动透传。

    Args:
        registry: 原 ToolRegistry。
        allowed: 允许的工具名列表（空 = 不过滤）。
        skill_name: Skill 名称（用于错误消息）。

    Returns:
        过滤后的新 ToolRegistry 实例。

    Raises:
        SkillDependencyError: 白名单中某个工具不存在。
    """
    if not allowed:
        return registry

    try:
        return registry.definitions_filtered(allowed)
    except SkillDependencyError as e:
        if skill_name:
            raise SkillDependencyError(f"Skill '{skill_name}': {e}") from e
        raise


class SkillExecutor:
    """Skill 执行器。

    持有 catalog、runtime、registry、provider 引用，对外暴露 execute / execute_inline / execute_fork。
    """

    def __init__(
        self,
        catalog: SkillLoader,
        runtime,
        registry: ToolRegistry,
        provider,
        workspace: str | Path = "",
    ) -> None:
        self._catalog = catalog
        self._runtime = runtime
        self._registry = registry
        self._provider = provider
        self._workspace = Path(workspace) if workspace else Path.cwd()
        self._agent = None  # 由 set_agent 注入（inline 模式需要）

    def set_agent(self, agent) -> None:
        """注入主 Agent 引用（inline 模式激活 SOP 用）。"""
        self._agent = agent

    # ── Public API ──────────────────────────────────────────────────

    async def execute_inline(self, skill_name: str, args: str, ui: UI) -> None:
        """inline 模式：渲染 SOP → 激活到 Agent → 注入消息触发回合。

        Args:
            skill_name: Skill 名称。
            args: 用户传入的参数。
            ui: UI 协议实现。
        """
        if self._agent is None:
            ui.error("Skill executor not initialized (agent not set)")
            return

        skill = self._catalog.get(skill_name)
        if skill is None:
            ui.error(f"Unknown skill: {skill_name}")
            return

        body = render_body(skill, args)
        self._agent.activate_skill(skill_name, body)
        await ui.inject_and_send(f"/{skill_name}", body)

    async def execute_fork(self, skill_name: str, args: str) -> str:
        """fork 模式：独立子会话执行，返回模型输出文本。

        Args:
            skill_name: Skill 名称。
            args: 用户传入的参数。

        Returns:
            子 Agent 的最终输出文本。出错时返回错误描述字符串。
        """
        skill = self._catalog.get(skill_name)
        if skill is None:
            return f"[skill {skill_name} failed: unknown skill]"

        body = render_body(skill, args)

        try:
            # 工具过滤
            fork_registry = filter_tool_registry(
                self._registry,
                skill.meta.allowed_tools,
                skill_name,
            )
        except SkillDependencyError as e:
            return f"[skill {skill_name} failed: {e}]"

        # 子 Agent 的 provider
        from llm.client import LLMClient

        if skill.meta.model:
            provider = LLMClient.create_with_model(self._provider, skill.meta.model)
        else:
            provider = self._provider

        # 构造 fork ConversationManager
        from conversation.manager import ConversationManager

        fork_conv = ConversationManager(system_prompt="")

        # 按 fork_context 装填历史
        context = skill.meta.fork_context
        if context == "recent":
            await _copy_recent_history(fork_conv, agent=None)
        elif context == "full":
            await _copy_full_summary(fork_conv, agent=None)

        # 注入渲染后的 SOP
        fork_conv.add_user_message(body)

        # 构造子 Agent
        from core.agent.agent import Agent
        from core.agent.config import AgentConfig
        from core.agent.runtime import SessionRuntime
        from core.context_compression.state import SessionContext
        from core.tool.context import ExecutionContext

        fork_session = SessionContext(
            session_id=f"fork-{skill_name}",
            session_dir=str(
                self._workspace / ".codeforge" / "sessions" / f"fork-{skill_name}"
            ),
            spill_dir=str(
                self._workspace
                / ".codeforge"
                / "sessions"
                / f"fork-{skill_name}"
                / "tool-results"
            ),
        )
        fork_runtime = SessionRuntime(
            session=fork_session,
            context_window=self._runtime.context_window,
        )
        # 继承主 Agent 的审批通道：fork 也是"父在 `await` 等结果"的前台形态，
        # 审批理应能冒泡到界面（与 AgentTool 的前台子 Agent 一致）。
        # 不继承的话 `arming_approval` 永远走不到通道分支，SKill 的审批只能被
        # 策略代答 —— 用户就看不到"这个 skill 想跑什么命令"。
        _parent_ctx = getattr(self._agent, "_exec_ctx", None)
        fork_exec_ctx = ExecutionContext(
            cwd=self._workspace,
            session_id=f"fork-{skill_name}",
            approval_upgrader=getattr(_parent_ctx, "approval_upgrader", None),
        )

        fork_agent = Agent(
            registry=fork_registry,
            llm_client=provider,
            exec_ctx=fork_exec_ctx,
            conversation=fork_conv,
            config=AgentConfig(max_iterations=25),
            runtime=fork_runtime,
        )
        # 身份名：审批弹窗的来源标注（`HITLRequired.origin`）与 span 归属都读它。
        # 不设的话，用户跑 `/review` 时弹窗只说"要跑 bash"、**不知道是哪个 skill 要的**
        # —— 审批上下文不完整就谈不上知情决策（对齐 Copilot CLI 那条）。
        # 这跟 `AgentTool` 对子 Agent 做的是同一件事，属"来源身份"机制的入口接线。
        fork_agent.set_agent_name(f"skill:{skill_name}")

        try:
            # 复用 SubAgent 统一循环：任务已作为首条 user 消息装填到 fork_conv，
            # 传 task="" 让 run_to_completion 跳过 add_user。
            #
            # ⚠️ 审批应答机制必须装（`arming_approval`，规格见 spec 附录 B）：
            # 这个 fork Agent 原先**三种应答机制全无**，于是 skill 一旦跑一条非安全
            # 白名单的命令（如 `pytest`、`git commit`）就会**无限期挂死**。
            # 更阴的是下面 `except asyncio.CancelledError` 会把取消吞成
            # `"[skill ... cancelled]"` **字符串返回** —— 从外面**看不出**它卡了。
            # 挂上之后：有通道就冒泡到界面问，没有就按主 Agent 的授权范围代答。
            from core.agent.sub_agent import arming_approval, run_to_completion

            with arming_approval(fork_agent, parent=self._agent):
                final_text = await run_to_completion(fork_agent, fork_conv, task="")

            # 写回 token 用量
            if fork_agent._total_usage:
                for key in ("input_tokens", "output_tokens"):
                    self._runtime.usage_anchor += fork_agent._total_usage.get(key, 0)

            return (
                final_text.strip()
                if final_text.strip()
                else f"[skill {skill_name} completed with no output]"
            )

        except asyncio.CancelledError:
            fork_agent.cancel()
            return f"[skill {skill_name} cancelled]"
        except Exception as e:
            logger.exception("Skill '%s' fork execution failed", skill_name)
            return f"[skill {skill_name} failed: {e}]"


# ── Internal helpers ──────────────────────────────────────────────


async def _copy_recent_history(
    fork_conv: ConversationManager,
    agent: object | None,
) -> None:
    """复制主对话最近 5 条 user/assistant 消息到 fork 对话。"""
    # 从 agent 获取主对话消息
    if agent is not None and hasattr(agent, "_conversation"):
        main_conv = agent._conversation
        messages = getattr(main_conv, "messages", [])
        recent = [
            m for m in messages[-5:] if getattr(m, "role", "") in ("user", "assistant")
        ]
        for m in recent:
            role = getattr(m, "role", "user")
            content = getattr(m, "content", "")
            if role == "user":
                fork_conv.add_user_message(content)
            else:
                fork_conv.add_assistant_message(content)


async def _copy_full_summary(
    fork_conv: ConversationManager,
    agent: object | None,
) -> None:
    """对主对话做简要摘要后注入 fork 对话。"""
    if agent is not None and hasattr(agent, "_conversation"):
        main_conv = agent._conversation
        messages = getattr(main_conv, "messages", [])
        if messages:
            # 简单摘要：取前几条关键消息
            preview = []
            for m in messages[:10]:
                role = getattr(m, "role", "user")
                content = getattr(m, "content", "")
                preview.append(f"[{role}]: {content[:200]}")
            summary = "\n".join(preview)
            fork_conv.add_user_message(f"## Previous conversation summary\n\n{summary}")
