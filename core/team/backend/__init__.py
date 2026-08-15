"""运行后端包 —— Backend Protocol / SpawnRequest / new_backend 工厂。

屏蔽 tmux / iterm2 / in-process 的 spawn 差异。三种实现各一个子模块。
`SpawnRequest.sub_agent / conv / task_mgr` 字段类型用 `Any`，避免在 backend 包反向依赖
`agent` 包形成环。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from core.team.backend.detect import backend_display_name, detect_backend
from core.team.types import BackendType


@dataclass
class SpawnRequest:
    """派生一个队员所需的全部参数。"""

    team_name: str
    member_name: str
    agent_id: str
    worktree_path: str
    session_dir: str
    agent_type: str = ""
    model: str = ""
    initial_prompt: str = ""
    plan_mode_required: bool = False

    # in-process 专用 —— 同进程后端直接复用这三个对象。
    sub_agent: Any = None  # agent.Agent
    conv: Any = None  # conversation.ConversationManager
    task_mgr: Any = None  # task.BackgroundTaskManager


class Backend(Protocol):
    """队员执行后端抽象。"""

    def type(self) -> BackendType: ...

    async def spawn(self, req: SpawnRequest) -> tuple[str, str]:
        """启动一个新队员，返回 (pane_id, agent_id)。"""
        ...

    async def wake(self, pane_id: str, agent_id: str) -> None:
        """消息到达时唤醒目标 pane（in-process 为 no-op）。"""
        ...

    async def kill(self, pane_id: str, agent_id: str) -> None:
        """终止 pane（Pane 后端）或取消 task（in-process）。"""
        ...


def new_backend(t: BackendType, **deps: Any) -> Backend:
    """按类型构造后端实例。deps 传入 task_mgr / wt_mgr 等所需依赖。"""
    if t is BackendType.TMUX:
        from core.team.backend.tmux import TmuxBackend

        return TmuxBackend(**deps)
    if t is BackendType.ITERM2:
        from core.team.backend.iterm2 import Iterm2Backend

        return Iterm2Backend(**deps)
    if t is BackendType.IN_PROCESS:
        from core.team.backend.inprocess import InProcessBackend

        return InProcessBackend(**deps)
    raise ValueError(f"未知后端类型: {t!r}")


__all__ = [
    "Backend",
    "SpawnRequest",
    "backend_display_name",
    "detect_backend",
    "new_backend",
]
