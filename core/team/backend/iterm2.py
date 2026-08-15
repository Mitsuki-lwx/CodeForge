"""iTerm2 后端 —— 在 iTerm2 split pane 里启动独立队员实例。

本机须有 it2 CLI 且 TERM_PROGRAM == iTerm.app（由 detect_backend 保证）。
命令构造与 tmux 后端同构（均含 --agent-id，initial_prompt 走 mailbox 预写）。
"""

from __future__ import annotations

import asyncio
import shlex
from typing import Any

from core.team.backend import SpawnRequest
from core.team.backend.tmux import _build_member_cmd
from core.team.types import BackendType


class Iterm2Backend:
    """iTerm2 split pane 后端。"""

    def __init__(self, it2: str = "it2", **_: Any) -> None:
        self._it2 = it2

    def type(self) -> BackendType:
        return BackendType.ITERM2

    async def spawn(self, req: SpawnRequest) -> tuple[str, str]:
        """用 it2 split 新 pane 启动队员，返回 (pane_id, agent_id)。"""
        cmd = _build_member_cmd(req)
        quoted_cmd = " ".join(shlex.quote(c) for c in cmd)
        proc = await asyncio.create_subprocess_exec(
            self._it2, "split", "--new-pane", "--command", quoted_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"it2 spawn 失败（rc={proc.returncode}）: "
                f"{stderr.decode(errors='replace') or stdout.decode(errors='replace')}"
            )
        pane_id = stdout.decode().strip() or ""
        return pane_id, req.agent_id

    async def wake(self, pane_id: str, agent_id: str) -> None:
        """向目标 pane 发空文本作为唤醒信号。"""
        if not pane_id:
            return
        proc = await asyncio.create_subprocess_exec(
            self._it2, "send-text", "--pane", pane_id, "",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    async def kill(self, pane_id: str, agent_id: str) -> None:
        """关闭目标 pane。"""
        if not pane_id:
            return
        proc = await asyncio.create_subprocess_exec(
            self._it2, "close-pane", "--pane", pane_id,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
