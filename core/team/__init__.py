"""团队协作系统 —— 包导出。

把 Team / Manager / BackendType 等顶层 API 集中导出，供 tui / agent_tool 等消费。
"""

from __future__ import annotations

from core.team.types import (
    BackendType,
    InProcessTeammateNoSpawnError,
    MemberExistsError,
    MemberNotFoundError,
    Team,
    TeamError,
    TeamHasActiveMembersError,
    TeammateInfo,
    TeamNotFoundError,
)

__all__ = [
    "BackendType",
    "InProcessTeammateNoSpawnError",
    "MemberExistsError",
    "MemberNotFoundError",
    "Team",
    "TeamError",
    "TeamHasActiveMembersError",
    "TeamNotFoundError",
    "TeammateInfo",
]
