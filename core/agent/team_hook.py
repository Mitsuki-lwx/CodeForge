"""团队队员上下文 —— TeamHook Protocol / TeamSpawnRequest / TeammateContext。

`agent` 包通过 `TeamHook` 委托给 `team` 包处理 Agent 工具的 team_name 分支（避免环）。
队员执行期间，其上下文挂在 `contextvars.ContextVar` 上，供 Loop 头部读邮箱与协作工具
定位当前 Team。mailbox 访问用闭包注入（`read_unread` / `mark_read`），不直接 import
mailbox 包（避免 agent → team 环）。
"""

from __future__ import annotations

import contextvars
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class IncomingMessage:
    """agent 包内轻量消息视图（独立于 mailbox.Message，避免环）。"""

    from_: str
    to: str
    type: str
    summary: str = ""
    content: str = ""
    payload: dict[str, Any] | None = None


@dataclass
class TeamSpawnRequest:
    """Agent 工具把 team_name 分支的入参交给 TeamHook。"""

    team_name: str
    member_name: str
    prompt: str
    description: str = ""
    subagent_type: str = ""
    model: str = ""
    plan_mode_required: bool = False


class TeamHook(Protocol):
    """agent 包委托给 team 包的接口。"""

    async def spawn_teammate(self, req: TeamSpawnRequest) -> str:
        """派生一个队员，返回 final_text（task_id / 成员 JSON 描述）。"""
        ...

    def is_teammate_context(self) -> tuple[str, str, bool]:
        """返回 (member_name, backend_type, is_in_team)；无队员上下文时返回 ("", "", False)。"""
        ...


@dataclass
class TeammateContext:
    """一个队员的执行上下文。"""

    team_name: str
    member_name: str
    agent_id: str
    backend_type: str = "in-process"

    # mailbox 访问闭包（由 team 包在 spawn 时注入，避免环）。
    read_unread: Callable[[], Awaitable[tuple[list[int], list[IncomingMessage]]]] = (
        lambda: _noop_read()
    )
    mark_read: Callable[[list[int]], Awaitable[None]] = (
        lambda indices: _noop_mark(indices)
    )

    # 定位当前 Team 的闭包。
    get_team: Callable[[], Any] | None = None

    def __post_init__(self) -> None:
        self._token: Any = None

    def install(self) -> None:
        """把本上下文压入 ContextVar 栈。"""
        self._token = _CURRENT_TEAMMATE.set(self)

    def uninstall(self) -> None:
        """恢复进入前的上下文状态。"""
        if self._token is not None:
            _CURRENT_TEAMMATE.reset(self._token)
            self._token = None


async def _noop_read() -> tuple[list[int], list[IncomingMessage]]:
    return [], []


async def _noop_mark(indices: list[int]) -> None:
    pass

_CURRENT_TEAMMATE: contextvars.ContextVar[TeammateContext | None] = (
    contextvars.ContextVar("codeforge_teammate", default=None)
)


def current_teammate() -> TeammateContext | None:
    """取当前队员上下文（Loop 头部读邮箱 / 协作工具定位 Team 用）。"""
    return _CURRENT_TEAMMATE.get()


def with_teammate_context(tc: TeammateContext) -> TeammateContext:
    """入栈并返回该上下文（配合 try/finally 调 uninstall）。"""
    tc.install()
    return tc


__all__ = [
    "IncomingMessage",
    "TeamHook",
    "TeamSpawnRequest",
    "TeammateContext",
    "current_teammate",
    "with_teammate_context",
]
