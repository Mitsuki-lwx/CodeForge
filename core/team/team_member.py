"""pane 队友自治循环 —— `python -m main --team-member`。

Pane 后端 spawn 的子进程不启动 TUI，而是跑本模块的循环：
1. chdir 到 worktree（权限沙箱根落到成员工作目录）
2. 从 team config 找到自己的 mailbox
3. 主循环：读未读 → 分流（text 拼任务 / shutdown_request 优雅退出）→ run_to_completion → 通知 Lead idle
4. stdin reader：任何回车（tmux send-keys）唤醒 → 立即轮询 mailbox

Agent 构造通过 `agent_factory: (worktree_path, member_name, agent_id, prompt) -> (agent, conv)` 注入，
便于单测用 fake；默认工厂做真实构造（需完整 LLM wire）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from core.team.manager import Manager

logger = logging.getLogger(__name__)

# 主循环轮询超时（秒）：无回车信号时兜底。
POLL_TIMEOUT = 2.0


def run_team_member_entry(args: Any) -> None:
    """CLI 入口：chdir 到 worktree 后跑 asyncio 循环。"""
    wt = args.worktree
    if wt:
        os.chdir(wt)
    home = str(Path.home())
    manager = Manager(home_dir=home, wt_mgr=None, task_mgr=None, reg=None)
    asyncio.run(
        run_team_member(
            manager=manager,
            team_name=args.team,
            member_name=args.member,
            agent_id=args.agent_id,
            worktree_path=wt or os.getcwd(),
            session_dir=args.session_dir or "",
            agent_factory=None,  # 默认用真实构造（当前 entry 未 wire 完整 LLM，实跑见 checklist）
        )
    )


async def run_team_member(
    manager: Any,
    team_name: str,
    member_name: str,
    agent_id: str,
    worktree_path: str,
    session_dir: str = "",
    agent_factory: Callable[[Any, str, str, str], tuple[Any, Any]] | None = None,
    notify_lead: bool = True,
) -> None:
    """pane 队友主循环（F19a）。agent_factory 供单测注入 fake。"""
    team = manager.get(team_name)
    if team is None:
        logger.error("[team-member] team %r not found", team_name)
        return

    from core.team.mailbox import Box

    mailbox = Box(team.mailbox_dir)
    factory = agent_factory or _default_factory

    while True:
        indices, msgs = await mailbox.read_unread(agent_id)
        if not msgs:
            # 无消息：睡一个小周期兜底轮询（真实环境会被 stdin 回车唤醒提前打断）
            try:
                await asyncio.wait_for(_blocked(), timeout=POLL_TIMEOUT)
            except TimeoutError:
                pass
            continue

        # 分流消息
        for m in msgs:
            t = m.type.value
            if t == "shutdown_request":
                logger.info("[team-member] %s: shutdown_request → 退出", member_name)
                return
            # text 任务消息 → 跑到底
            if t == "text":
                agent, conv = factory(worktree_path, member_name, agent_id, m.content)
                await _run_and_notify(
                    manager, team, mailbox, agent, conv,
                    member_name, agent_id, notify_lead,
                )
        await mailbox.mark_read(agent_id, indices)


async def _blocked() -> None:
    # 占位：被 asyncio.wait_for 包裹，超时即认为无唤醒信号。
    await asyncio.sleep(POLL_TIMEOUT + 1)


async def _run_and_notify(
    manager: Any,
    team: Any,
    mailbox: Any,
    agent: Any,
    conv: Any,
    member_name: str,
    agent_id: str,
    notify_lead: bool,
) -> None:
    from core.agent.sub_agent import run_to_completion

    try:
        await run_to_completion(agent, conv, "", None)
    except BaseException as e:  # noqa: BLE001 —— pane 内崩溃打印不退出
        print(f"[team-member] run failed: {e}", file=sys.stderr)
    # 通知 Lead idle
    if notify_lead:
        from core.team.mailbox.message import Message, MessageType

        try:
            await mailbox.write(
                team.lead_agent_id,
                Message(
                    from_=member_name,
                    to=team.lead_agent_id,
                    type=MessageType.TEXT,
                    summary=f"{member_name} idle",
                    content=f"agent {agent_id} finished work, available for new tasks",
                ),
            )
        except Exception as e:  # noqa: BLE001 —— 通知失败不阻断
            logger.warning("notify lead failed: %s", e)
    # 标 inactive（跨进程 reload 兜底）
    try:
        if hasattr(manager, "set_member_active"):
            await manager.set_member_active(team, member_name, False)
    except Exception as e:  # noqa: BLE001 —— 状态更新失败仅告警
        logger.debug("member inactive update failed: %s", e)


# ── 默认 agent 工厂（真实构造，需完整 LLM wire）────────────────

def _default_factory(
    worktree_path: str, member_name: str, agent_id: str, prompt: str
) -> tuple[Any, Any]:
    """构造真实队友 Agent + Conv。

    需要调用方已 wire 好 provider / conversation。当前 entry 未注入完整 client，
    实跑（checklist D 场景 1）时由完整的 --team-member wire 覆盖本函数。
    """
    raise RuntimeError(
        "team-member 默认工厂未 wire 完整 LLM client——真实 pane 子进程请注入 agent_factory"
    )


__all__ = ["run_team_member", "run_team_member_entry"]
