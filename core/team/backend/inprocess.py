"""in-process 后端 —— 同进程 asyncio task 里跑队员。

复用 `BackgroundTaskManager.launch` 起一个后台子 Agent，在 asyncio task 内跑
`run_to_completion`。pane_id 恒为空，用 agent_id（task_id）作为目标 id。
"""

from __future__ import annotations

from typing import Any

from core.team.backend import SpawnRequest
from core.team.types import BackendType


class InProcessBackend:
    """同进程协程后端。"""

    def __init__(self, task_mgr: Any = None, **_: Any) -> None:
        self._task_mgr = task_mgr

    def type(self) -> BackendType:
        return BackendType.IN_PROCESS

    async def spawn(self, req: SpawnRequest) -> tuple[str, str]:
        """在同一事件循环里 launch 一个后台任务，返回 (pane_id="", agent_id)。"""
        task_mgr = req.task_mgr or self._task_mgr
        if task_mgr is None:
            raise RuntimeError("in-process 后端缺少 task_mgr")
        if req.sub_agent is None or req.conv is None:
            raise RuntimeError("in-process 后端要求预构造 sub_agent 与 conv")

        task_id = await task_mgr.launch(
            req.sub_agent,
            req.conv,
            name=req.member_name,
            task_text=req.initial_prompt,
        )
        return "", task_id

    async def wake(self, pane_id: str, agent_id: str) -> None:
        # 同进程：下一轮 Agent Loop 自动读邮箱，无需主动唤醒。
        return None

    async def kill(self, pane_id: str, agent_id: str) -> None:
        if self._task_mgr is None:
            return
        await self._task_mgr.stop(agent_id)
