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
        fork_exec_ctx = ExecutionContext(
            cwd=self._workspace,
            session_id=f"fork-{skill_name}",
        )

        fork_agent = Agent(
            registry=fork_registry,
            llm_client=provider,
            exec_ctx=fork_exec_ctx,
            conversation=fork_conv,
            config=AgentConfig(max_iterations=25),
            runtime=fork_runtime,
        )

        try:
            # 收集子 Agent 输出
            result_parts: list[str] = []
            from core.agent.events import AgentError, AgentFinished, TextDelta

            async for event in fork_agent.run(body):
                if isinstance(event, TextDelta):
                    result_parts.append(event.text)
                elif isinstance(event, AgentError):
                    result_parts.append(f"\n[Error: {event.message}]")
                elif isinstance(event, AgentFinished):
                    break

            final_text = "".join(result_parts).strip()

            # 写回 token 用量
            if fork_agent._total_usage:
                for key in ("input_tokens", "output_tokens"):
                    self._runtime.usage_anchor += fork_agent._total_usage.get(key, 0)

            return (
                final_text
                if final_text
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
