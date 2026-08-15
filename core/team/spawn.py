"""spawn_teammate —— Agent 工具 team_name 分支的团队派生主流程。

复用 `_build_sub_agent` 类似的子 Agent 构造，但：
- 用 `build_teammate_tools` 生成队友专用工具集（team-bound 协作工具）。
- 强制 `dont_ask=True`（队友无 TUI 接 ApprovalRequest，F39a）。
- 注入 `<team-context>` system reminder（F40）。
- 按后端分流：in-process 走 task_mgr.launch；pane 后端把 initial_prompt 预写 mailbox 后 spawn。
"""

from __future__ import annotations

import logging
import secrets
from pathlib import Path
from typing import Any

from core.team.types import (
    BackendType,
    TeammateInfo,
    TeamNotFoundError,
)

logger = logging.getLogger(__name__)


def build_team_context_reminder(team: Any, info: TeammateInfo) -> str:
    """构造 `<team-context>` system reminder 文本（F40）。"""
    members = ", ".join(
        f"{m.name}({m.agent_type or 'member'})" for m in team.members
    ) or "（仅 Lead）"
    return (
        "<team-context>\n"
        f"team: {team.name}\n"
        f"你的成员名: {info.name}\n"
        f"你的 agent_id: {info.agent_id}\n"
        f"worktree 目录: {info.worktree_path}\n"
        f"当前团队成员: {members}\n"
        "</team-context>"
    )


async def spawn_teammate(
    manager: Any,
    parent_agent: Any,
    worktree_mgr: Any,
    task_mgr: Any,
    name_reg: Any,
    team_name: str,
    member_name: str,
    prompt: str,
    subagent_type: str = "",
    model: str = "",
    plan_mode_required: bool = False,
) -> str:
    """派生一个队员，返回 final_text（成员信息 JSON 摘要）。

    Args:
        manager: core.team.manager.Manager。
        parent_agent: 调用 Agent 工具的主 Agent（Lead）。
        worktree_mgr: core.worktree manager。
        task_mgr: core.task.BackgroundTaskManager。
        name_reg: AgentNameRegistry。
    """
    team = manager.get(team_name)
    if team is None:
        raise TeamNotFoundError(f"未找到团队: {team_name!r}")

    backend = team.backend

    # ---- 1. 创建队员 worktree ----
    member_name = _unique_member_name(team, member_name or (subagent_type or "worker"))
    wt_path, branch = await _create_worktree(
        worktree_mgr, team.sanitized_name, member_name
    )

    # ---- 2. 预生成 agent_id + 申请 session_dir ----
    agent_id = f"agent-{secrets.token_hex(7)}"
    session_dir = _new_session_dir(wt_path)

    # ---- 3. 构造队友专用工具集 ----
    from core.team.tools import build_teammate_tools

    parent_tools: list[Any] = list(parent_agent._registry.list())
    member_tools = build_teammate_tools(
        parent_tools=parent_tools,
        team_manager=manager,
        team_name=team.sanitized_name,
        agent_id=agent_id,
        agent_name=member_name,
        backend_type=backend.value,
    )

    # ---- 4. 构造子 Agent + Conv（强制 dont_ask=True）----
    sub_agent = _build_teammate_agent(
        manager=manager,
        parent_agent=parent_agent,
        member_tools=member_tools,
        worktree_path=wt_path,
        system_prompt_extra="",
    )
    sub_conv = _build_teammate_conv(
        prompt=prompt,
        subagent_type=subagent_type,
        team=team,
        info=_mk_info(member_name, agent_id, wt_path, branch, backend, session_dir,
                       plan_mode_required),
    )
    # 挂 worktree 供 run_to_completion 清理
    sub_agent._worktree_session = getattr(parent_agent, "_worktree_session", None)
    sub_agent._wt_manager = worktree_mgr

    info = TeammateInfo(
        name=member_name,
        agent_id=agent_id,
        agent_type=subagent_type,
        model=model,
        worktree_path=wt_path,
        branch=branch,
        backend_type=backend,
        pane_id="",
        is_active=True,
        plan_mode_required=plan_mode_required,
        session_dir=session_dir,
    )

    # ---- 5. 按后端分流 ----
    if backend is BackendType.IN_PROCESS:
        await manager.add_member(team, info)
        if name_reg is not None:
            name_reg.register(member_name, agent_id)
        try:
            task_id = await task_mgr.launch(
                sub_agent, sub_conv, name=member_name, task_text=prompt
            )
        except Exception:
            await manager.remove_member(team, member_name)
            if name_reg is not None:
                name_reg.unregister(member_name)
            raise
        return f"队员 {member_name} 已派生（in-process，task_id={task_id}）"

    # ---- 6. pane 后端：initial_prompt 预写 mailbox 后 spawn ----
    from core.team.backend import Backend, SpawnRequest, new_backend
    from core.team.mailbox import Box
    from core.team.mailbox.message import Message, MessageType

    mailbox = Box(team.mailbox_dir)
    await mailbox.write(
        agent_id,
        Message(
            from_="lead",
            to=member_name,
            type=MessageType.TEXT,
            summary=_truncate_for_summary(prompt),
            content=prompt,
        ),
    )
    req = SpawnRequest(
        team_name=team.sanitized_name,
        member_name=member_name,
        agent_id=agent_id,
        worktree_path=wt_path,
        session_dir=session_dir,
        agent_type=subagent_type,
        model=model,
        initial_prompt=prompt,
        plan_mode_required=plan_mode_required,
    )
    backend: Backend = new_backend(backend, task_mgr=task_mgr)
    pane_id, _ = await backend.spawn(req)
    info.pane_id = pane_id
    await manager.add_member(team, info)
    if name_reg is not None:
        name_reg.register(member_name, agent_id)
    return f"队员 {member_name} 已派生（{backend.value}，pane_id={pane_id}）"


def _truncate_for_summary(prompt: str, limit: int = 12) -> str:
    words = prompt.split()
    return " ".join(words[:limit])


def _unique_member_name(team: Any, base: str) -> str:
    names = {m.name for m in team.members}
    candidate = base
    counter = 2
    while candidate in names:
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


async def _create_worktree(worktree_mgr: Any, sanitized: str, member: str) -> tuple[str, str]:
    """创建队员 worktree，返回 (绝对路径, 分支名)。"""
    wt_name = f"team-{sanitized}/{member}"
    branch = f"worktree-team-{sanitized}+{member}"
    if worktree_mgr is None:
        raise RuntimeError("worktree_manager 未注入，无法创建队友 worktree")
    wt = await worktree_mgr.create(wt_name, owner="agent")
    path = str(getattr(wt, "path", "") or "")
    return path, branch


def _new_session_dir(worktree_path: str) -> str:
    """为队员生成独立 session 目录（存 conversation.jsonl）。"""
    return str(Path(worktree_path) / ".codeforge" / "session")


def _mk_info(
    name: str, agent_id: str, wt_path: str, branch: str,
    backend: BackendType, session_dir: str, plan_mode_required: bool,
) -> TeammateInfo:
    return TeammateInfo(
        name=name, agent_id=agent_id, worktree_path=wt_path, branch=branch,
        backend_type=backend, session_dir=session_dir,
        plan_mode_required=plan_mode_required, is_active=True,
    )


def _build_teammate_agent(
    manager: Any,
    parent_agent: Any,
    member_tools: list[Any],
    worktree_path: str,
    system_prompt_extra: str,
) -> Any:
    """构造队友 Agent（dont_ask=True + team-bound 工具 registry）。"""
    from pathlib import Path

    from core.agent.agent import Agent
    from core.agent.config import AgentConfig
    from core.agent.runtime import SessionRuntime
    from core.context_compression.state import new_session_context
    from core.permissions.modes import PermissionMode
    from core.tool.context import ExecutionContext
    from core.tool.registry import ToolRegistry

    sub_registry = ToolRegistry()
    for tool in member_tools:
        try:
            sub_registry.register(tool)
        except Exception:  # noqa: BLE001, S110 —— 单个工具失败跳过
            pass

    parent = parent_agent
    workspace = str(
        getattr(parent, "_exec_ctx", ExecutionContext(cwd=Path.cwd())).cwd
    )
    work_cwd = Path(worktree_path) if worktree_path else Path(workspace)

    sub_session = new_session_context(str(work_cwd))
    sub_runtime = SessionRuntime(
        session=sub_session,
        context_window=getattr(getattr(parent, "_runtime", None), "context_window", 200000),
        notes=None,
    )

    sub_agent = Agent(
        registry=sub_registry,
        llm_client=getattr(parent, "_client", None),
        exec_ctx=ExecutionContext(cwd=work_cwd),
        conversation=__import__(
            "conversation.manager", fromlist=["ConversationManager"]
        ).ConversationManager(),
        config=AgentConfig(max_iterations=25),
        runtime=sub_runtime,
        system_prompt=system_prompt_extra or None,
        max_turns=25,
        permission_mode=PermissionMode.DEFAULT,
        dont_ask=True,  # F39a：队友无 TUI 接 ApprovalRequest
        hooks=getattr(parent, "_hooks", None),
    )
    return sub_agent


def _build_teammate_conv(
    prompt: str,
    subagent_type: str,
    team: Any,
    info: TeammateInfo,
) -> Any:
    """构造队友 Conv：任务作为首条 user 消息 + 注入 `<team-context>` reminder。"""
    from conversation.manager import ConversationManager

    conv = ConversationManager()
    conv.add_user_message(prompt)
    conv.add_system_reminder(build_team_context_reminder(team, info))
    return conv


def _team_reminder_sync(conv: Any, team: Any, info: TeammateInfo) -> Any:
    conv.add_system_reminder(build_team_context_reminder(team, info))
    return conv


__all__ = ["build_team_context_reminder", "spawn_teammate"]
