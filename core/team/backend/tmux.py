"""tmux 后端 —— 在独立 tmux pane 里启动完整 CodeForge 实例做强隔离。

`--agent-id` 是关键：Lead spawn 时已生成 agent_id 直接传给子进程，子进程无需读
Lead 尚未写完的 config.json 找自己。initial_prompt 不走命令行（由 spawn_teammate
预写入 mailbox，见 spec F13）。

在 tmux 会话内 spawn 走 split-window；若在 tmux 会话外但本机有 tmux，回落 detached
new-session；失败报错而非回退 in-process（不静默降级）。
"""

from __future__ import annotations

import asyncio
import os
import shlex
from typing import Any

from core.team.backend import SpawnRequest
from core.team.types import BackendType

# 队员 pane 启动的 CLI 命令（模块入口，见 spec F15 / tasks T29）。
_MEMBER_CMD = ["python", "-m", "main"]


def _build_member_cmd(req: SpawnRequest) -> list[str]:
    """构造被 spawn 的队员命令行（含 --agent-id）。"""
    cmd = list(_MEMBER_CMD)
    cmd += ["--team-member", "--team", req.team_name, "--member", req.member_name]
    cmd += ["--agent-id", req.agent_id]
    cmd += ["--session-dir", req.session_dir, "--worktree", req.worktree_path]
    if req.agent_type:
        cmd += ["--agent-type", req.agent_type]
    if req.model:
        cmd += ["--model", req.model]
    if req.plan_mode_required:
        cmd += ["--plan-mode"]
    return cmd


class TmuxBackend:
    """tmux pane 后端。"""

    def __init__(self, **_: Any) -> None:
        self._tmux = "tmux"

    def type(self) -> BackendType:
        return BackendType.TMUX

    async def spawn(self, req: SpawnRequest) -> tuple[str, str]:
        """在 tmux 里 split 一个新 pane 启动队员，返回 (pane_id, agent_id)。"""
        cmd = _build_member_cmd(req)
        # shell 引号化，供 -- 后传给 tmux 的单个命令字符串
        quoted_cmd = " ".join(shlex.quote(c) for c in cmd)

        if os.environ.get("TMUX"):
            # 当前在 tmux 会话内：横向 split，-P 打印 pane id，-F 指定格式
            args = [
                self._tmux, "split-window", "-h", "-P", "-F", "#{pane_id}",
                "--", quoted_cmd,
            ]
        else:
            # 在 tmux 会话外：detached 新会话
            args = [
                self._tmux, "new-session", "-d",
                "--", quoted_cmd,
            ]

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"tmux spawn 失败（rc={proc.returncode}）: "
                f"{stderr.decode(errors='replace') or stdout.decode(errors='replace')}"
            )
        pane_id = stdout.decode().strip() or ""
        return pane_id, req.agent_id

    async def wake(self, pane_id: str, agent_id: str) -> None:
        """向目标 pane 发一个回车，触发其 stdin reader 去 mailbox 轮询。"""
        if not pane_id:
            return
        proc = await asyncio.create_subprocess_exec(
            self._tmux, "send-keys", "-t", pane_id, "", "Enter",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    async def kill(self, pane_id: str, agent_id: str) -> None:
        """kill 掉目标 pane；pane 不存在时静默忽略。"""
        if not pane_id:
            return
        proc = await asyncio.create_subprocess_exec(
            self._tmux, "kill-pane", "-t", pane_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
